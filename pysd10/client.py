"""Async BLE client for the Epson SD-10 (built on bleak)."""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as dt
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable, Optional, Sequence, Union

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from . import protocol as p
from .color import ILLUMINANTS, Lab, _FORMULA_ALIASES, _illuminant_name, delta_e
from .protocol import Cmd, ColorGroup, Event, MeasurementResult, Method, Setting, Status

log = logging.getLogger("pysd10")

DEFAULT_APP_ID_FILE = Path.home() / ".pysd10_app_id"
_LEGACY_APP_ID_FILE = Path.home() / ".sd10_app_id"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class SD10Error(Exception):
    """Base class for all errors raised by this package."""


class SD10Timeout(SD10Error):
    pass


class DeviceNotFound(SD10Error):
    pass


class DeviceRefused(SD10Error):
    """The device answered NG/TO (e.g. it is busy or held by another host)."""


class MeasurementFailed(SD10Error):
    def __init__(self, msg: str, status: Optional[Status] = None):
        super().__init__(msg)
        self.status = status


class CoverClosed(MeasurementFailed):
    """The cover (shutter) is closed. The SD-10 would not measure; it just re-sends its previous result."""


class CalibrationNeeded(MeasurementFailed):
    """The device reports that it needs a white calibration first (close the cover, then calibrate())."""


class CoverOpen(SD10Error):
    """Calibration needs the cover closed (the white reference tile is inside it)."""


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
@dataclass
class FoundDevice:
    address: str
    name: str
    rssi: int
    device: BLEDevice


async def scan(timeout: float = 8.0) -> list[FoundDevice]:
    """Return SD-10s advertising nearby, strongest signal first."""
    found = await BleakScanner.discover(timeout=timeout, service_uuids=[p.ADV_SERVICE_UUID], return_adv=True)
    out = [FoundDevice(d.address, adv.local_name or d.name or "Unknown", adv.rssi, d) for d, adv in found.values()]
    return sorted(out, key=lambda f: -f.rssi)


async def find_device(address: Optional[str] = None, timeout: float = 8.0) -> BLEDevice:
    if address:
        dev = await BleakScanner.find_device_by_address(address, timeout=timeout)
        if dev is None:
            raise DeviceNotFound(f"device {address} not found")
        return dev
    found = await scan(timeout)
    if not found:
        raise DeviceNotFound("no SD-10 found (is it powered on and not connected to a phone?)")
    return found[0].device


def load_app_id(path: Path = DEFAULT_APP_ID_FILE) -> bytes:
    """16-byte host id sent when opening a session. Like the app, generate once and keep it
    (an id saved by versions before 0.3, in ~/.sd10_app_id, is reused)."""
    for candidate in (path, _LEGACY_APP_ID_FILE):
        try:
            app_id = bytes.fromhex(candidate.read_text().strip())
        except (OSError, ValueError):
            continue
        if len(app_id) == 16:
            if candidate != path:
                with contextlib.suppress(OSError):
                    path.write_text(app_id.hex())
            return app_id
    app_id = secrets.token_bytes(16)
    with contextlib.suppress(OSError):
        path.write_text(app_id.hex())
    return app_id


@dataclass
class PatchResult:
    """One measured patch of a scan / chart comparison."""
    patch: int                     # 1-based position in the chart (palette colour number)
    result: MeasurementResult      # the raw reading (spectrum etc.)
    lab: Lab                       # under the comparison illuminant/observer
    expected: Optional[Lab] = None
    delta_e: Optional[float] = None
    ok: Optional[bool] = None      # delta_e <= tolerance


@dataclass
class RowResult:
    """A scanned row. ``passed`` follows the app: mean dE of the row <= tolerance."""
    row: int
    patches: list[PatchResult]
    illuminant: str
    observer: int
    formula: Optional[str] = None
    tolerance: Optional[float] = None

    @property
    def mean_delta_e(self) -> Optional[float]:
        des = [p.delta_e for p in self.patches if p.delta_e is not None]
        return sum(des) / len(des) if des else None

    @property
    def max_delta_e(self) -> Optional[float]:
        des = [p.delta_e for p in self.patches if p.delta_e is not None]
        return max(des) if des else None

    @property
    def passed(self) -> Optional[bool]:
        m = self.mean_delta_e
        return None if m is None or self.tolerance is None else m <= self.tolerance

    @property
    def failed_patches(self) -> list[PatchResult]:
        return [p for p in self.patches if p.ok is False]


def _formula(name: str) -> str:
    f = name.upper()
    f = _FORMULA_ALIASES.get(f, f)
    if f not in p.DIFF_FORMULAS:
        raise ValueError(f"unknown formula {name!r}")
    return f


def _row_result(row, first_patch, results, expected, illuminant, observer, formula, tolerance) -> RowResult:
    if expected is not None and len(results) != len(expected):
        log.warning("expected %d patches, the device returned %d", len(expected), len(results))
    patches = []
    for i, r in enumerate(results):
        lab = r.lab_under(illuminant, observer)
        exp = expected[i] if expected is not None and i < len(expected) else None
        de = delta_e(exp, lab, formula or "DE2000") if exp is not None else None
        ok = None if de is None or tolerance is None else de <= tolerance
        patches.append(PatchResult(first_patch + i, r, lab, exp, de, ok))
    return RowResult(row, patches, illuminant, observer, formula if expected is not None else None,
                     tolerance if expected is not None else None)


@dataclass
class DeviceInfo:
    name: str
    firmware: str
    serial: str
    status: Status


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class SD10:
    """Connection to one SD-10. Normally obtained from :func:`connect`, which also opens a session.

    Lower-level use::

        async with BleakClient(address) as ble:
            dev = SD10(ble)
            await dev.start()
            await dev.open_session()
            ...
            await dev.close_session()
    """

    def __init__(self, client: BleakClient, app_id: Optional[bytes] = None):
        self.client = client
        self.app_id = app_id or load_app_id()
        self._rx = bytearray()
        self._rx_signal = asyncio.Event()
        self._in_command = False  # BluetoothWork.mNotifyEventMode: True = buffer reply, False = deliver events
        self._lock = asyncio.Lock()
        self.events: asyncio.Queue[int] = asyncio.Queue()
        self._chunk = 20
        self.session_open = False

    @property
    def address(self) -> str:
        return self.client.address

    @property
    def is_connected(self) -> bool:
        return self.client.is_connected

    # ---- transport -------------------------------------------------------
    async def start(self) -> None:
        """Enable notifications. Call once after the BLE connection is up."""
        mtu = getattr(self.client, "mtu_size", 23) or 23
        self._chunk = max(20, mtu - 3)
        await self.client.start_notify(p.NOTIFY_CHAR_UUID, self._on_notify)
        log.debug("notifications on, MTU %d -> %d-byte writes", mtu, self._chunk)

    def _on_notify(self, _char, data: bytearray) -> None:
        log.debug("<- %s", bytes(data).hex(" "))
        self._rx += data
        if not self._in_command:
            self._drain_events()
        self._rx_signal.set()

    def _drain_events(self) -> None:
        while self._rx and self._rx[0] != 0x00:
            self._emit_event(self._rx.pop(0))
        if self._rx and not self._in_command:  # stale/unsolicited frame outside a transaction
            log.debug("discarding unsolicited bytes %s", bytes(self._rx).hex(" "))
            self._rx.clear()

    def _emit_event(self, b: int) -> None:
        log.debug("event 0x%02X", b)
        self.events.put_nowait(b)

    async def _write(self, data: bytes) -> None:
        log.debug("-> %s", data.hex(" "))
        for i in range(0, len(data), self._chunk):
            await self.client.write_gatt_char(p.WRITE_CHAR_UUID, data[i:i + self._chunk], response=True)

    async def _read_frame(self, timeout: float) -> tuple[int, int, bytes]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            while self._rx and self._rx[0] != 0x00:  # events in front of the reply
                self._emit_event(self._rx.pop(0))
            if len(self._rx) >= p.HEADER_LEN:
                magic, cid, typ, n = p.HEADER.unpack_from(self._rx)
                if magic != p.MAGIC:
                    raise SD10Error(f"bad frame header {bytes(self._rx[:8]).hex(' ')}")
                if len(self._rx) >= p.HEADER_LEN + n:
                    payload = bytes(self._rx[p.HEADER_LEN:p.HEADER_LEN + n])
                    del self._rx[:p.HEADER_LEN + n]
                    return cid, typ, payload
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise SD10Timeout(f"timeout waiting for reply (have {bytes(self._rx).hex(' ')})")
            self._rx_signal.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._rx_signal.wait(), remaining)

    async def transact(self, cmd: bytes, timeout: float = 5.0) -> tuple[int, int, bytes]:
        """Send one framed command, return ``(id, type, payload)`` of the reply, and ACK it."""
        async with self._lock:
            self._rx.clear()
            self._in_command = True
            ok = False
            try:
                await self._write(cmd)
                cid, typ, payload = await self._read_frame(timeout)
                ok = True
            finally:
                self._in_command = False
                await self._write(bytes([p.ACK_OK if ok else p.ACK_NG]))
                self._drain_events()
            if cid != cmd[2]:
                log.debug("reply id 0x%02X != request id 0x%02X", cid, cmd[2])
            return cid, typ, payload

    async def _expect_ok(self, cmd: bytes, what: str, timeout: float = 5.0) -> None:
        _, _, payload = await self.transact(cmd, timeout)
        reply = payload.rstrip(b"\0").decode("ascii", "replace")
        if reply != "OK":
            raise DeviceRefused(f"{what}: device replied {reply!r}")

    async def wait_event(self, *wanted: int, timeout: Optional[float] = 20.0) -> int:
        """Wait for one of the given event codes (any event if none given); ``timeout=None`` waits forever."""
        if timeout is None:
            while True:
                evt = await self.events.get()
                if not wanted or evt in wanted:
                    return evt
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise SD10Timeout(f"timeout waiting for event(s) {[hex(w) for w in wanted]}")
            try:
                evt = await asyncio.wait_for(self.events.get(), remaining)
            except asyncio.TimeoutError:
                continue
            if not wanted or evt in wanted:
                return evt

    def clear_events(self) -> None:
        while not self.events.empty():
            self.events.get_nowait()

    # ---- session ---------------------------------------------------------
    async def open_session(self, client_type: int = p.CLIENT_PERMIT_SA, retry_for: float = 25.0) -> None:
        """Take control of the device (Controller::Open). Retries for up to 25 s like the app."""
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        while True:
            _, _, payload = await self.transact(p.encode_control_device(True, self.app_id, client_type))
            reply = payload.rstrip(b"\0").decode("ascii", "replace")
            if reply == "OK":
                self.session_open = True
                return
            if reply in ("NG", "TO"):
                raise DeviceRefused(f"device refused control ({reply})")
            if loop.time() - t0 > retry_for:
                raise DeviceRefused(f"open: unexpected reply {payload!r}")
            await asyncio.sleep(1.0)

    async def close_session(self, client_type: int = p.CLIENT_PERMIT_SA) -> None:
        await self.transact(p.encode_control_device(False, self.app_id, client_type))
        self.session_open = False

    # ---- queries / settings ---------------------------------------------
    async def get_status(self) -> Status:
        _, _, payload = await self.transact(p.frame(Cmd.STATUS, p.T_GET, b"\x00"))
        return p.parse_status(payload)

    async def _get_string(self, cmd_id: int) -> str:
        _, _, payload = await self.transact(p.frame(cmd_id, p.T_GET, b"\x01"))
        return payload.split(b"\0", 1)[0].decode("utf-8", "replace")

    async def get_name(self) -> str:
        return await self._get_string(Cmd.DEVICE_NAME)

    async def get_firmware_version(self) -> str:
        return await self._get_string(Cmd.FW_VERSION)

    async def get_serial(self) -> str:
        return await self._get_string(Cmd.SERIAL)

    async def get_info(self) -> DeviceInfo:
        return DeviceInfo(await self.get_name(), await self.get_firmware_version(), await self.get_serial(),
                          await self.get_status())

    async def get_setting(self, setting: Setting) -> int:
        _, _, payload = await self.transact(p.frame(setting, p.T_GET, b"\x01"))
        return payload[0] if payload else -1

    async def set_setting(self, setting: Setting, value: int) -> None:
        await self._expect_ok(p.frame(setting, p.T_SET, bytes([value])), f"set {Setting(setting).name}")

    async def set_name(self, name: str) -> None:
        raw = name.encode("utf-8")[:16]
        await self._expect_ok(p.frame(Cmd.DEVICE_NAME, p.T_SET, raw.ljust(16, b"\0")), "set name")

    async def set_clock(self, when: Optional[dt.datetime] = None) -> None:
        """Set the device clock (default: now, local time)."""
        await self._expect_ok(p.encode_datetime(when or dt.datetime.now()), "set clock")

    async def get_measurement_mode(self) -> dict[int, bytes]:
        """Raw TLV of the current measurement mode."""
        _, _, payload = await self.transact(p.frame(Cmd.MODE_GET, p.T_GET, b"\x01"))
        return p.parse_tlv(payload)

    async def set_measurement_mode(self, **kwargs) -> None:
        """Low-level mode setter; keyword arguments as :func:`pysd10.protocol.encode_measurement_mode`."""
        await self._expect_ok(p.encode_measurement_mode(**kwargs), "set measurement mode")

    async def notify_result(self, success: bool) -> None:
        """Tell the device whether the last (scan/chart) measurement passed, so it can show OK / NG."""
        await self._expect_ok(p.frame(Cmd.NOTIFY_RESULT, p.T_SET, b"\x01" if success else b"\x02"), "notify result")

    async def set_standalone(self) -> None:
        """Hand the device back to its own UI (the app does this when it is done)."""
        await self._expect_ok(p.frame(Cmd.STANDALONE, p.T_EXEC, b"\x01"), "set standalone mode")

    # ---- measuring -------------------------------------------------------
    async def _check_ready(self) -> Status:
        """What the app checks before every measurement: calibration done, cover open."""
        status = await self.get_status()
        if status.calibration_status == 0:
            raise CalibrationNeeded("The SD-10 needs calibrating: close the sensor cover, then calibrate.", status)
        if status.shutter_status == 1:
            raise CoverClosed("The SD-10's cover is closed - open it to measure.", status)
        return status

    async def measure(self, illuminant: Union[str, int] = "D50", observer: int = 2, m_factor: int = 0,
                      save: bool = True, timeout: float = 20.0) -> MeasurementResult:
        """Take one spot measurement. The result's ``.lab`` uses the given illuminant/observer.

        Raises :class:`CoverClosed` if the cover is closed (the device would otherwise silently return its
        previous reading) and :class:`MeasurementFailed` if the device reports a failed reading.
        """
        if observer not in (2, 10):
            raise ValueError("observer must be 2 or 10")
        if m_factor not in (0, 1, 2):
            raise ValueError("m_factor must be 0, 1 or 2")
        self._last_settings = dict(illuminant=illuminant, observer=observer, m_factor=m_factor, save=save)
        await self._check_ready()
        await self.set_measurement_mode(method=Method.SPOT_SINGLE, save_result=save, m_factor=m_factor,
                                        light_source=ILLUMINANTS[_illuminant_name(illuminant)],
                                        viewing_angle=observer)
        results = await self.trigger_measurement(timeout)
        if not results:
            raise MeasurementFailed("device returned no result")
        result = results[0]
        if not result.ok:
            status = await self.get_status()
            raise MeasurementFailed(f"measurement failed: {', '.join(status.errors) or status}", status)
        return result

    async def trigger_measurement(self, timeout: float = 20.0) -> list[MeasurementResult]:
        """Start a measurement with the current mode, wait for it, return the result cache."""
        self.clear_events()
        await self._expect_ok(p.frame(Cmd.MEASURE, p.T_EXEC, b"\x01"), "execute measurement")
        await self.wait_event(Event.FINISH_MEASURING, timeout=timeout)
        return await self.read_results()

    async def read_results(self) -> list[MeasurementResult]:
        """Read the device's result cache (the most recent measurement)."""
        _, _, size_payload = await self.transact(p.frame(Cmd.RESULT_CACHE, p.T_SIZE, b"\x00"))
        expected = int.from_bytes(size_payload, "little")
        _, _, payload = await self.transact(p.frame(Cmd.RESULT_CACHE, p.T_GET, b"\x00"))
        if expected and len(payload) != expected:
            log.debug("cache size-check said %d bytes, got %d", expected, len(payload))
        return p.parse_result_cache(payload)

    # ---- colour groups (palettes) on the device ----------------------------
    async def get_groups(self) -> list[ColorGroup]:
        """The colour groups stored on the device (empty slots are left out)."""
        await self.transact(p.encode_get_groups(size_check=True), timeout=30.0)
        _, _, payload = await self.transact(p.encode_get_groups(), timeout=30.0)
        return p.parse_groups(payload)

    async def set_group(self, group: ColorGroup) -> ColorGroup:
        """Store ``group`` in its slot (replacing whatever is there). Returns the group as stored
        (with a random ``hardware_id`` filled in if it was 0)."""
        if not group.hardware_id:
            used = {g.hardware_id for g in await self.get_groups()}
            group = dataclasses.replace(group, hardware_id=next(
                h for h in iter(lambda: secrets.randbelow(65534) + 1, None) if h not in used))
        await self._expect_ok(p.encode_set_groups([group]), "set group", timeout=30.0)
        return group

    async def _get_group(self, slot: int) -> ColorGroup:
        for g in await self.get_groups():
            if g.slot == slot:
                return g
        raise SD10Error(f"group slot {slot} is empty")

    async def add_to_group(self, slot: int, colors) -> ColorGroup:
        """Append colours to the palette in ``slot`` (keeping its name and settings). ``colors`` may be
        L*a*b* triples or MeasurementResults; results are converted under the palette's own
        illuminant/observer. Returns the updated group."""
        group = await self._get_group(slot)
        new = [c.lab_under(group.illuminant, group.observer) if isinstance(c, MeasurementResult) else Lab(*c[:3])
               for c in colors]
        if len(group.colors) + len(new) > p.GROUP_MAX_COLORS:
            raise ValueError(f"palette '{group.name}' has {len(group.colors)} colours; adding {len(new)} "
                             f"would exceed {p.GROUP_MAX_COLORS}")
        return await self.set_group(dataclasses.replace(group, colors=group.colors + new))

    async def remove_from_group(self, slot: int, indexes) -> ColorGroup:
        """Remove colours from the palette in ``slot`` by 0-based position. A palette needs at least one
        colour; use delete_group() to remove it entirely. Returns the updated group."""
        group = await self._get_group(slot)
        drop = set(indexes)
        if not drop <= set(range(len(group.colors))):
            raise ValueError(f"palette '{group.name}' has colours 0..{len(group.colors) - 1}")
        keep = [c for i, c in enumerate(group.colors) if i not in drop]
        if not keep:
            raise ValueError("a palette needs at least one colour - use delete_group() to clear the slot")
        return await self.set_group(dataclasses.replace(group, colors=keep))

    async def delete_group(self, slot: int) -> None:
        """Clear a group slot (1..4)."""
        await self._expect_ok(p.encode_set_groups(clear_slots=[slot]), "delete group", timeout=30.0)

    # ---- stored history --------------------------------------------------
    async def history_count(self, after_id: int = 0) -> int:
        """Number of readings stored on the device with an id greater than ``after_id`` (max 100 per query;
        the SD-10 keeps its most recent 100 readings)."""
        _, _, payload = await self.transact(p.encode_history_check(after_id))
        return p.parse_history_check(payload)[1]

    async def get_history(self, after_id: int = 0, limit: Optional[int] = None,
                          progress: Optional[Callable[[int], None]] = None) -> list[MeasurementResult]:
        """Download readings stored on the device, oldest first.

        ``after_id``: only readings with a larger record id (``result.identifier``) - use the id of the last
        reading you already have to fetch just the new ones. ``limit``: stop after this many.
        ``progress(n)`` is called with the running total after each batch.
        """
        out: list[MeasurementResult] = []
        while limit is None or len(out) < limit:
            available = await self.history_count(after_id)
            if available == 0:
                break
            n = min(available, p.HISTORY_BATCH_MAX, (limit - len(out)) if limit is not None else available)
            _, _, payload = await self.transact(p.encode_history_get(after_id, n), timeout=10.0 + 0.2 * n)
            batch = p.parse_history(payload)
            if not batch:
                break
            out.extend(batch)
            if progress:
                progress(len(out))
            if batch[-1].identifier <= after_id:  # defensive: never loop on a device that does not advance
                break
            after_id = batch[-1].identifier
        return out

    async def calibrate(self, timeout: float = 30.0) -> Status:
        """Run white calibration. The cover must be **closed** (the white tile is inside it); raises
        :class:`CoverOpen` otherwise. Returns the status afterwards (``status.calibrated``)."""
        status = await self.get_status()
        if status.shutter_status != 1:
            raise CoverOpen("Close the sensor cover, then start calibration.")
        self.clear_events()
        await self._expect_ok(p.frame(Cmd.CALIBRATE, p.T_EXEC, b"\x01"), "calibrate")
        await self.wait_event(Event.FINISH_CALIBRATION, timeout=timeout)
        status = await self.get_status()
        if not status.calibrated:
            raise MeasurementFailed(f"calibration failed: {', '.join(status.errors) or status}", status)
        return status

    # ---- scan (row) measurement and chart comparison ----------------------
    # Not available in the GUI. Flow as in the app (Q1/i event 194 -> h1 -> Q0.e.w + execute -> Q0.d):
    # the user puts the SD-10 on the first patch of a row and presses its button (MEASURE_BUTTON_PUSH),
    # the host sets the scan mode and starts the measurement, the user slides the device along the row
    # to the margin, the device reports FINISH_MEASURING and the host reads one result per patch, then
    # tells the device whether the row passed (NotifyColorMeasurementSuccessOrFailure).
    SCAN_MAX_PATCHES = 18

    async def _run_scan(self, mode: dict, wait_for_button: bool, timeout: float,
                        button_timeout: Optional[float]) -> list[MeasurementResult]:
        await self._check_ready()
        if wait_for_button:
            self.clear_events()
            log.info("waiting for the measure button (place the SD-10 at the start of the row)")
            await self.wait_event(Event.MEASURE_BUTTON_PUSH, timeout=button_timeout)
            await self._check_ready()
        await self.set_measurement_mode(**mode)
        results = await self.trigger_measurement(timeout)
        if not results:
            raise MeasurementFailed("device returned no result")
        if not all(r.ok for r in results):
            status = await self.get_status()
            await self.notify_result(False)
            raise MeasurementFailed(f"scan failed: {', '.join(status.errors) or status}", status)
        return results

    async def scan_row(self, expected: Optional[Sequence[Sequence[float]]] = None, *, patches: Optional[int] = None,
                       row: int = 1, illuminant: Union[str, int] = "D50", observer: int = 2, m_factor: int = 0,
                       formula: str = "DE2000", tolerance: Optional[float] = None, wait_for_button: bool = True,
                       timeout: float = 60.0, button_timeout: Optional[float] = None) -> "RowResult":
        """Scan one row of up to 18 colour patches.

        Give either ``expected`` (the row's target L*a*b* colours, one per patch - the app always sends
        these) or just the number of ``patches``. With ``tolerance`` the device runs its scan-comparison
        mode (patches separated by a border) and each patch is judged against ``expected``; without it,
        it is a plain line scan. With ``wait_for_button`` (default) this waits for the user to press the
        device's button at the start of the row; otherwise the scan starts immediately.
        """
        n = len(expected) if expected is not None else patches
        if not n or not 1 <= n <= self.SCAN_MAX_PATCHES:
            raise ValueError(f"a scanned row has 1..{self.SCAN_MAX_PATCHES} patches")
        if tolerance is not None and expected is None:
            raise ValueError("a tolerance needs the expected colours")
        labs = [tuple(c[:3]) for c in expected] if expected is not None else [(0.0, 0.0, 0.0)] * n
        light = ILLUMINANTS[_illuminant_name(illuminant)]
        if tolerance is None:
            mode = dict(method=Method.SCAN, save_result=False, m_factor=m_factor, light_source=light,
                        viewing_angle=observer, patch_num=n, patch_border=False, labs=labs, line_no=row)
        else:
            mode = dict(method=Method.SCAN_COMPARISON, save_result=False, m_factor=m_factor, light_source=light,
                        viewing_angle=observer, diff_formula=p.DIFF_FORMULAS[_formula(formula)],
                        diff_threshold=tolerance, patch_num=n, patch_border=True, labs=labs, line_no=row)
        results = await self._run_scan(mode, wait_for_button, timeout, button_timeout)
        out = _row_result(row, 1, results, [Lab(*c) for c in labs] if expected is not None else None,
                          _illuminant_name(illuminant), observer, _formula(formula), tolerance)
        await self.notify_result(out.passed is not False)
        return out

    async def scan_palette_row(self, slot: int, row: int, *, patches_per_row: int = 10, wait_for_button: bool = True,
                               timeout: float = 60.0, button_timeout: Optional[float] = None) -> "RowResult":
        """Scan row ``row`` (1-based) of a printed chart of the palette stored in ``slot``, and compare each
        patch with the palette colour it should be. The palette's own illuminant/observer, formula and
        tolerance are used. ``patches_per_row`` is the chart layout (the app uses 10; at most 18)."""
        if not 1 <= patches_per_row <= self.SCAN_MAX_PATCHES:
            raise ValueError(f"patches_per_row must be 1..{self.SCAN_MAX_PATCHES}")
        group = await self._get_group(slot)
        start = (row - 1) * patches_per_row + 1
        if row < 1 or start > len(group.colors):
            raise ValueError(f"palette '{group.name}' has {len(group.colors)} colours: rows 1.."
                             f"{-(-len(group.colors) // patches_per_row)} at {patches_per_row} per row")
        n = min(patches_per_row, len(group.colors) - start + 1)
        mode = dict(method=Method.SCAN_GROUP, save_result=True, group_no=slot, patch_num=n,
                    start_patch_no=start, line_no=row)
        results = await self._run_scan(mode, wait_for_button, timeout, button_timeout)
        out = _row_result(row, start, results, group.colors[start - 1:start - 1 + n], group.illuminant,
                          group.observer, group.diff_formula, group.tolerance)
        await self.notify_result(bool(out.passed))
        return out

    async def scan_palette(self, slot: int, *, patches_per_row: int = 10, timeout: float = 60.0
                           ) -> AsyncIterator["RowResult"]:
        """Scan a whole printed chart of a stored palette row by row (the user presses the button at the
        start of each row). Yields one :class:`RowResult` per row and tells the device the chart is done
        at the end::

            async for row in dev.scan_palette(1):
                print(row.row, "passed" if row.passed else "FAILED", row.mean_delta_e)
        """
        group = await self._get_group(slot)
        rows = -(-len(group.colors) // patches_per_row)
        for row in range(1, rows + 1):
            yield await self.scan_palette_row(slot, row, patches_per_row=patches_per_row, timeout=timeout)
        await self.finish_chart()

    async def measure_palette_patch(self, slot: int, patch: int, *, wait_for_button: bool = False,
                                    timeout: float = 20.0) -> "PatchResult":
        """Spot-measure one patch and compare it with colour ``patch`` (1-based) of the palette in ``slot``
        (the app's "group spot comparison"); uses the palette's settings and tolerance."""
        group = await self._get_group(slot)
        if not 1 <= patch <= len(group.colors):
            raise ValueError(f"palette '{group.name}' has colours 1..{len(group.colors)}")
        mode = dict(method=Method.SPOT_GROUP, save_result=True, group_no=slot, start_patch_no=patch)
        results = await self._run_scan(mode, wait_for_button, timeout, None)
        out = _row_result(1, patch, results[:1], [group.colors[patch - 1]], group.illuminant, group.observer,
                          group.diff_formula, group.tolerance)
        await self.notify_result(bool(out.passed))
        return out.patches[0]

    async def finish_chart(self) -> None:
        """Tell the device a chart/palette comparison is finished (NotifyGroupColormeasurementIsCompleted)."""
        await self._expect_ok(p.frame(Cmd.NOTIFY_GROUP_DONE, p.T_SET, b"\x01"), "finish chart")

    async def button_measurements(self, illuminant: Union[str, int, None] = None, observer: Optional[int] = None,
                                  m_factor: Optional[int] = None, save: Optional[bool] = None
                                  ) -> AsyncIterator[MeasurementResult]:
        """Yield a result every time the device's measure button is pressed::

            async for result in dev.button_measurements():
                print(result.lab)

        While a host session is open the SD-10 does not measure by itself: a button press only sends
        MEASURE_BUTTON_PUSH, and the host has to run the measurement (that is what the app does, see
        Q1/i.java event 194). Settings default to those of the last :meth:`measure` call, else D50/2/M0.
        Presses within 1.5 s of the previous one are ignored, as in the app.
        """
        last = getattr(self, "_last_settings", {})
        settings = dict(illuminant=illuminant if illuminant is not None else last.get("illuminant", "D50"),
                        observer=observer or last.get("observer", 2),
                        m_factor=m_factor if m_factor is not None else last.get("m_factor", 0),
                        save=save if save is not None else last.get("save", True))
        loop = asyncio.get_running_loop()
        last_press = float("-inf")
        while True:
            evt = await self.wait_event(Event.MEASURE_BUTTON_PUSH, Event.FINISH_MEASURING, timeout=None)
            if evt == Event.FINISH_MEASURING:      # a measurement finished without us starting it
                for result in await self.read_results():
                    yield result
                continue
            now = loop.time()
            if now - last_press < 1.5:
                continue
            last_press = now
            log.debug("button pressed; starting measurement with %s", settings)
            try:
                result = await self.measure(**settings)
            except MeasurementFailed as ex:  # e.g. cover closed: report it and keep listening
                log.warning("%s", ex)
                continue
            yield result


@contextlib.asynccontextmanager
async def connect(address: Optional[str] = None, *, scan_timeout: float = 8.0, app_id: Optional[bytes] = None,
                  standalone_on_exit: bool = True, attempts: int = 3,
                  retry_delay: float = 1.5) -> AsyncIterator[SD10]:
    """Connect to an SD-10 (strongest one found if ``address`` is None) and open a session::

        async with pysd10.connect() as dev:
            result = await dev.measure()
            print(result.lab)

    Connection setup is retried (``attempts`` times): on Windows, enabling notifications right after
    connecting sometimes fails with "operation was canceled", especially just after a previous
    connection was closed. The Epson app retries its BLE init for the same reason.
    """
    target = await find_device(address, scan_timeout)
    last_error: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        log.info("connecting to %s (%s), attempt %d/%d", target.address, target.name, attempt, attempts)
        ble = BleakClient(target)
        try:
            await ble.connect()
            dev = SD10(ble, app_id)
            await dev.start()
            await dev.open_session()
            break
        except DeviceRefused:
            with contextlib.suppress(Exception):
                await ble.disconnect()
            raise
        except (OSError, BleakError, asyncio.TimeoutError, SD10Timeout) as ex:
            last_error = ex
            log.info("connection setup failed (%s); retrying", ex)
            with contextlib.suppress(Exception):
                await ble.disconnect()
            if attempt < attempts:
                await asyncio.sleep(retry_delay)
    else:
        raise SD10Error(f"could not connect to {target.address} after {attempts} attempts: {last_error}") \
            from last_error

    try:
        yield dev
    finally:
        if ble.is_connected:
            if standalone_on_exit:
                with contextlib.suppress(Exception):
                    await dev.set_standalone()
            with contextlib.suppress(Exception):
                await dev.close_session()
        with contextlib.suppress(Exception):
            await ble.disconnect()
