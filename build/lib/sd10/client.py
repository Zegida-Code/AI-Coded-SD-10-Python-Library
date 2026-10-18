"""Async BLE client for the Epson SD-10 (built on bleak)."""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional, Union

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from . import protocol as p
from .color import ILLUMINANTS, _illuminant_name
from .protocol import Cmd, Event, MeasurementResult, Method, Setting, Status

log = logging.getLogger("sd10")

DEFAULT_APP_ID_FILE = Path.home() / ".sd10_app_id"


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
    """16-byte host id sent when opening a session. Like the app, generate once and keep it."""
    try:
        return bytes.fromhex(path.read_text().strip())
    except (OSError, ValueError):
        app_id = secrets.token_bytes(16)
        with contextlib.suppress(OSError):
            path.write_text(app_id.hex())
        return app_id


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
        """Low-level mode setter; keyword arguments as :func:`sd10.protocol.encode_measurement_mode`."""
        await self._expect_ok(p.encode_measurement_mode(**kwargs), "set measurement mode")

    async def set_standalone(self) -> None:
        """Hand the device back to its own UI (the app does this when it is done)."""
        await self._expect_ok(p.frame(Cmd.STANDALONE, p.T_EXEC, b"\x01"), "set standalone mode")

    # ---- measuring -------------------------------------------------------
    async def measure(self, illuminant: Union[str, int] = "D50", observer: int = 2, m_factor: int = 0,
                      save: bool = True, timeout: float = 20.0) -> MeasurementResult:
        """Take one spot measurement. The result's ``.lab`` uses the given illuminant/observer.

        Raises :class:`MeasurementFailed` if the device reports a failed reading.
        """
        if observer not in (2, 10):
            raise ValueError("observer must be 2 or 10")
        if m_factor not in (0, 1, 2):
            raise ValueError("m_factor must be 0, 1 or 2")
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

    async def calibrate(self, timeout: float = 30.0) -> Status:
        """Run white calibration (device must be on its calibration tile). Returns the new status."""
        self.clear_events()
        await self._expect_ok(p.frame(Cmd.CALIBRATE, p.T_EXEC, b"\x01"), "calibrate")
        await self.wait_event(Event.FINISH_CALIBRATION, timeout=timeout)
        return await self.get_status()

    async def button_measurements(self) -> AsyncIterator[MeasurementResult]:
        """Yield a result every time a measurement finishes on the device (e.g. button press)::

            async for result in dev.button_measurements():
                print(result.lab)
        """
        while True:
            await self.wait_event(Event.FINISH_MEASURING, timeout=None)
            for result in await self.read_results():
                yield result


@contextlib.asynccontextmanager
async def connect(address: Optional[str] = None, *, scan_timeout: float = 8.0, app_id: Optional[bytes] = None,
                  standalone_on_exit: bool = True) -> AsyncIterator[SD10]:
    """Connect to an SD-10 (strongest one found if ``address`` is None) and open a session::

        async with sd10.connect() as dev:
            result = await dev.measure()
            print(result.lab)
    """
    target = await find_device(address, scan_timeout)
    log.info("connecting to %s (%s)", target.address, target.name)
    async with BleakClient(target) as ble:
        dev = SD10(ble, app_id)
        await dev.start()
        await dev.open_session()
        try:
            yield dev
        finally:
            if ble.is_connected:
                if standalone_on_exit:
                    with contextlib.suppress(Exception):
                        await dev.set_standalone()
                with contextlib.suppress(Exception):
                    await dev.close_session()
