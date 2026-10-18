"""Wire protocol of the Epson SD-10: constants, frame encoding and reply parsing (no I/O).

Recreated from the Epson Spectrometer Android app (libmunsell-lib.so, EPSPM::CommandBuilder /
ResponseParser / Device / Controller) and checked against the native code under emulation.

Frame (both directions, little-endian)::

    00 43 | id:u8 | type:u8 | len:u32 | payload[len]

type: 0x10 size-check, 0x11 get, 0x12 execute, 0x13 set (host -> device);
0x01 reply, 0x00 size-check reply (device -> host).

Every command is one exchange: host sends the command, device sends one reply frame, host
acknowledges with ``FF`` (``FE`` if the reply was unusable). Non-zero bytes the device sends outside a
frame are asynchronous events (see ``EVT_*``). Set/execute replies carry ASCII ``OK``, ``NG`` or ``TO``.
"""
from __future__ import annotations

import datetime as dt
import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional, Sequence

from .color import ILLUMINANTS, ILLUMINANT_NAMES, WAVELENGTHS, Lab, LCh, spectrum_to_lab

# --------------------------------------------------------------------------- #
# BLE GATT layout (com.epson.munselllib.ble.BluetoothUuid)
# --------------------------------------------------------------------------- #
ADV_SERVICE_UUID = "4d756e73-656c-6cde-92b1-3b0cb21f4bf7"   # advertised; used as scan filter
GATT_SERVICE_UUID = "49535343-fe7d-4ae5-8fa9-9fafd205e455"  # Microchip transparent UART
NOTIFY_CHAR_UUID = "49535343-1e4d-4bd9-ba61-23c647249616"   # device -> host
WRITE_CHAR_UUID = "49535343-8841-43f4-a8d4-ecbe34729bb3"    # host -> device

# --------------------------------------------------------------------------- #
# Framing
# --------------------------------------------------------------------------- #
MAGIC = b"\x00\x43"
HEADER = struct.Struct("<2sBBI")  # magic, id, type, payload length
HEADER_LEN = HEADER.size          # 8

T_SIZE, T_GET, T_EXEC, T_SET = 0x10, 0x11, 0x12, 0x13
T_REPLY, T_SIZE_REPLY = 0x01, 0x00
ACK_OK, ACK_NG = 0xFF, 0xFE


class Cmd(IntEnum):
    MEASURE = 0x01
    FW_VERSION = 0x03
    RESULT_CACHE = 0x05
    NVRAM = 0x07
    DEVICE_NAME = 0x08
    STATUS = 0x0B
    CALIBRATE = 0x0C
    DATETIME = 0x0D
    SERIAL = 0x0E
    BUZZER_VOLUME = 0x0F
    PANEL_DIRECTION = 0x10
    HISTORY_OFFSET = 0x13
    HISTORY = 0x14
    GROUP_SET = 0x15
    GROUP_GET = 0x16
    CONTROL = 0x19
    MODE_SET = 0x1A
    MODE_GET = 0x1B
    NOTIFY_RESULT = 0x1C
    STANDALONE = 0x1D
    NOTIFY_GROUP_DONE = 0x1E
    AUTO_POWER_OFF = 0x1F
    LANGUAGE = 0x20


class Setting(IntEnum):
    """One-byte device settings readable/writable with get_setting()/set_setting()."""
    BUZZER_VOLUME = Cmd.BUZZER_VOLUME      # 0 off, 1 quiet, 2 loud
    PANEL_DIRECTION = Cmd.PANEL_DIRECTION  # 0, 1 (90), 2 (180), 3 (270)
    AUTO_POWER_OFF = Cmd.AUTO_POWER_OFF    # 0 disabled, 1 10 min, 2 30 min, 3 60 min
    LANGUAGE = Cmd.LANGUAGE                # 0 ja, 1 en, 2 fr, 3 de, 4 it, 5 es, 6 pt, 8 tr, 9 nl, 10 ru, ...


class Event(IntEnum):
    FINISH_MEASURING = 0xC1
    MEASURE_BUTTON_PUSH = 0xC2
    FINISH_CALIBRATION = 0xC3
    CHANGED_STATUS = 0xC4


class Method(IntEnum):
    """EPSPM_METHOD (API values; encode_measurement_mode maps them to wire codes)."""
    SPOT_SINGLE = 1
    SPOT_COMPARISON = 2
    SPOT_GROUP = 3
    SCAN = 4
    SCAN_GROUP = 5
    CALIBRATION = 6
    SCAN_COMPARISON = 7


_METHOD_WIRE = {1: 1, 2: 2, 3: 2, 4: 5, 5: 3, 6: 4, 7: 3}

CLIENT_PERMIT_SA = 1     # EPSPM_CLIENTTYPE; the app always opens with 1
CLIENT_PROHIBIT_SA = 2

DIFF_FORMULAS = {"DE2000": 0, "DE94GA": 1, "DE94T": 2, "DE76": 3}

# Bit index in the status error bitmask -> EPSPM_STATUS_ERROR_* name (MunsellDef.java)
STATUS_ERRORS = {
    2: "FATAL_LV0_SC_TEMP_HIGH", 3: "FATAL_LV0_SC_TEMP_LOW", 4: "FATAL_LV0_SC_BATTERY_NO_RECEIVE",
    5: "FATAL_LV0_SC_BATTERY_CONNECT", 6: "FATAL_LV0_SC_BATTERY_TIMEOUT", 7: "FATAL_LV0_SC_BATTERY_UPERR",
    8: "FATAL_LV0_SC_BATTERY_FULL", 9: "FATAL_LV1_SC_LED_DRV", 10: "FATAL_LV1_SC_LED_FAIL",
    11: "FATAL_LV1_SC_ETALON_DEVICE_FAIL", 12: "FATAL_LV1_SC_ETALON_TH_FAIL", 13: "FATAL_LV1_SC_LED_TH_FAIL",
    14: "ALERT_DISCHARGE_TEMP_LOW_MUC", 15: "ALERT_DISCHARGE_TEMP_LOW_BATTERY",
    16: "ALERT_DISCHARGE_TEMP_HIGH_MUC", 17: "ALERT_DISCHARGE_TEMP_HIGH_BATTERY",
    18: "ALERT_ETALON_TH_HIGH_WARNING", 19: "ALERT_ETALON_TH_LOW_WARNING", 20: "ALERT_LED_TH_HIGH_WARNING",
    21: "ALERT_LED_TH_LOW_WARNING", 23: "ALERT_DEVICE_ERR_SERIAL_FLASH", 24: "ALERT_DEVICE_ERR_LED_DRIVER",
    25: "ALERT_DEVICE_ERR_TH_SENSOR", 26: "ALERT_DEVICE_ERR_BLE", 27: "ALERT_DEVICE_ERR_BCCC",
    28: "ALERT_DEVICE_ERR_RTC", 30: "ALERT_FWUPDATE_TEMP_HIGH", 32: "ALERT_WDT_ERR",
    33: "ALERT_FWUPDATE_TEMP_LOW", 34: "ALERT_CALIB_ERROR", 35: "INFORM_BATTERY_EMPTY_FAIL",
    36: "INFORM_BATTERY_CHARGE_HIGH_CANCEL", 37: "INFORM_BATTERY_CHARGE_HIGH_CTL",
    38: "INFORM_BATTERY_CHARGE_LOW_CANCEL", 39: "INFORM_BATTERY_CHARGE_LOW_CTL",
    40: "INFORM_BATTERY_DISCHARGE_HIGH_CANCEL", 41: "INFORM_BATTERY_DISCHARGE_HIGH_CTL",
    42: "INFORM_BATTERY_DISCHARGE_LOW_CANCEL", 43: "INFORM_BATTERY_DISCHARGE_LOW_CTL",
    44: "INFORM_BATTERY_EMPTY_LOW", 47: "INFORM_CALIB_SHUTTER_OPEN_PON_MEASUREMENT",
    48: "INFORM_CALIB_SHUTTER_OPEN_PON", 49: "INFORM_CALIB_SHUTTER_OPEN_MEASUREMENT",
    50: "INFORM_SCAN_MEASUREMENT_FAIL_SHORT_PATCH", 51: "INFORM_SCAN_MEASUREMENT_FAIL_TIMEOUT",
    52: "INFORM_SCAN_MEASUREMENT_FAIL_QUICK_MOTION", 54: "INFORM_DISTORY_DATA_FULL_COLOR",
    55: "INFORM_DISTORY_DATA_FULL_GROUP", 57: "INFORM_OTHER_MEASUREMENT_EXE_ERROR",
}

RESULT_RECORD_LEN = 144   # cache record
HISTORY_RECORD_LEN = 143  # history record (same layout without the leading "ok" byte)
HISTORY_BATCH_MAX = 100   # the device reports/returns at most 100 records per request


# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #
def frame(cmd_id: int, typ: int, payload: bytes = b"") -> bytes:
    return HEADER.pack(MAGIC, cmd_id, typ, len(payload)) + payload


def tlv(*items: tuple[int, bytes]) -> bytes:
    return b"".join(bytes([tag, len(val)]) + val for tag, val in items)


def parse_tlv(payload: bytes) -> dict[int, bytes]:
    out, i = {}, 0
    while i + 2 <= len(payload):
        tag, n = payload[i], payload[i + 1]
        out[tag] = payload[i + 2:i + 2 + n]
        i += 2 + n
    return out


def encode_control_device(start: bool, app_id: bytes, client_type: int = CLIENT_PERMIT_SA) -> bytes:
    """ControlDevice: start (open) or finish (close) a host session."""
    if len(app_id) != 16:
        raise ValueError("app_id must be 16 bytes")
    return frame(Cmd.CONTROL, T_EXEC, tlv((1, bytes([1 if start else 2])), (2, bytes([client_type])), (3, app_id)))


def encode_datetime(t: dt.datetime) -> bytes:
    if not 2020 <= t.year <= 2099:  # CreateSetDatetimeInfoCmd rejects YY outside 20..99
        raise ValueError("year must be 2020..2099")
    return frame(Cmd.DATETIME, T_SET, bytes([t.year - 2000, t.month, t.day, t.hour, t.minute, t.second]))


def encode_history_check(after_id: int) -> bytes:
    """How many stored records have an id greater than ``after_id`` (reply capped at 100)."""
    return frame(Cmd.HISTORY_OFFSET, T_GET, b"\x01" + struct.pack("<I", after_id))


def encode_history_get(after_id: int, count: int) -> bytes:
    """The next ``count`` stored records with id greater than ``after_id``, oldest first."""
    return frame(Cmd.HISTORY, T_EXEC, b"\x01" + struct.pack("<II", after_id, count))


def encode_measurement_mode(method: int = Method.SPOT_SINGLE, save_result: bool = True, m_factor: int = 0,
                            light_source: int = 0, viewing_angle: int = 2, diff_formula: int = 0,
                            diff_threshold: float = 0.0, group_no: int = 0, patch_num: int = 0,
                            start_patch_no: int = 0, patch_border: bool = False,
                            labs: Sequence[tuple[float, float, float]] = (), line_no: int = 0) -> bytes:
    """Mirror of CommandBuilder::CreateSetColorMeasurementModeCmd. ``light_source`` is the device code
    (see ``ILLUMINANTS``), ``viewing_angle`` 2 or 10."""
    if method not in _METHOD_WIRE:
        raise ValueError(f"unknown method {method}")
    items = [(0x01, bytes([_METHOD_WIRE[method]]))]
    if method != Method.CALIBRATION:
        items.append((0x02, bytes([2 if save_result else 1])))
        if method in (Method.SPOT_SINGLE, Method.SPOT_COMPARISON, Method.SCAN, Method.SCAN_COMPARISON):
            items += [(0x04, bytes([m_factor])), (0x05, bytes([light_source])), (0x06, bytes([viewing_angle]))]
        if method in (Method.SPOT_COMPARISON, Method.SCAN_COMPARISON):
            items += [(0x07, bytes([diff_formula])), (0x08, struct.pack("<f", diff_threshold * 10.0))]
        if method in (Method.SPOT_GROUP, Method.SCAN_GROUP):
            items.append((0x09, bytes([group_no])))
        if method in (Method.SCAN, Method.SCAN_GROUP, Method.SCAN_COMPARISON):
            items.append((0x0A, bytes([patch_num])))
        if method in (Method.SPOT_GROUP, Method.SCAN_GROUP):
            items.append((0x0B, bytes([start_patch_no])))
        if method in (Method.SCAN, Method.SCAN_COMPARISON):
            lab_bytes = b"".join(struct.pack("<3f", *(v * 100.0 for v in lab)) for lab in labs[:patch_num])
            items += [(0x0D, bytes([1 if patch_border else 0])), (0x0E, lab_bytes),
                      (0x0F, struct.pack("<H", line_no))]
    return frame(Cmd.MODE_SET, T_SET, tlv(*items))


# --------------------------------------------------------------------------- #
# Decoding
# --------------------------------------------------------------------------- #
@dataclass
class Status:
    device_status: int = 0        # 0 error, 4 idle
    error_status: int = 0         # bitmask, see STATUS_ERRORS
    calibration_status: int = 0   # 0 needed, 2 done, 255 other
    battery: int = 0              # percent
    shutter_status: int = 0       # 0 open, 1 closed
    charging_status: int = 0      # 0 not charging, 1 charging
    errors: list[str] = field(default_factory=list)

    @property
    def idle(self) -> bool:
        return self.device_status == 4

    @property
    def calibrated(self) -> bool:
        return self.calibration_status == 2

    @property
    def charging(self) -> bool:
        return self.charging_status == 1


def parse_status(payload: bytes) -> Status:
    t = parse_tlv(payload)

    def first(k: int) -> int:
        return t[k][0] if t.get(k) else 0
    s = Status(device_status=first(1), error_status=int.from_bytes(t.get(2, b""), "big"),
               calibration_status=first(3), battery=first(4), shutter_status=first(5), charging_status=first(6))
    s.errors = [STATUS_ERRORS.get(b, f"bit{b}") for b in range(64) if s.error_status >> b & 1]
    return s


@dataclass
class MeasurementResult:
    """One measurement. ``spectrum`` is reflectance at ``WAVELENGTHS`` (400-700 nm, 10 nm step)."""
    ok: bool
    timestamp: Optional[dt.datetime]
    spectrum: list[float]
    m_factor: int
    light_source: int        # device code, see ILLUMINANTS
    viewing_angle: int       # observer, 2 or 10
    diff_formula: int
    identifier: int          # device record id (little-endian u32)
    inspection_no: int
    patch_no: int
    group_id: int

    wavelengths = WAVELENGTHS

    @property
    def illuminant(self) -> str:
        """Illuminant recorded with the measurement (name)."""
        return ILLUMINANT_NAMES.get(self.light_source, "D50")

    @property
    def observer(self) -> int:
        return self.viewing_angle if self.viewing_angle in (2, 10) else 2

    @property
    def lab(self) -> Lab:
        """L*a*b* under the illuminant/observer the measurement was taken with."""
        return spectrum_to_lab(self.spectrum, self.illuminant, self.observer)

    @property
    def lch(self) -> LCh:
        return self.lab.lch

    def lab_under(self, illuminant: str = "D50", observer: int = 2) -> Lab:
        """L*a*b* of this spectrum under any supported illuminant/observer."""
        return spectrum_to_lab(self.spectrum, illuminant, observer)

    def spectrum_dict(self) -> dict[int, float]:
        return dict(zip(WAVELENGTHS, self.spectrum))

    def to_dict(self) -> dict:
        lab = self.lab
        return {"ok": self.ok, "timestamp": self.timestamp.isoformat() if self.timestamp else None,
                "illuminant": self.illuminant, "observer": self.observer, "m_factor": self.m_factor,
                "lab": {"L": lab.L, "a": lab.a, "b": lab.b}, "lch": dict(zip("LCh", lab.lch)),
                "spectrum": self.spectrum_dict(), "identifier": self.identifier,
                "inspection_no": self.inspection_no, "patch_no": self.patch_no, "group_id": self.group_id}


def parse_result_record(rec: bytes, from_history: bool = False) -> MeasurementResult:
    """Mirror of ResponseParser::GetColorMeasurementResult."""
    if from_history:            # history records omit the leading "gettingResult" byte
        rec = b"\x01" + rec
    if len(rec) < RESULT_RECORD_LEN:
        raise ValueError(f"short result record ({len(rec)} bytes)")
    ok, yy, mo, dd, hh, mi, ss = rec[0:7]
    try:
        ts: Optional[dt.datetime] = dt.datetime(2000 + yy, mo, dd, hh, mi, ss)
    except ValueError:
        ts = None
    spectrum = [round(v, 6) for v in struct.unpack_from("<31f", rec, 7)]
    m_factor, light, angle, formula = rec[0x83:0x87]
    ident, = struct.unpack_from("<I", rec, 0x87)
    insp, = struct.unpack_from("<H", rec, 0x8B)
    group, = struct.unpack_from("<H", rec, 0x8E)
    return MeasurementResult(bool(ok), ts, spectrum, m_factor, light, angle, formula, ident, insp, rec[0x8D], group)


# --------------------------------------------------------------------------- #
# Colour groups (palettes) stored on the device
# --------------------------------------------------------------------------- #
GROUP_SLOTS = (1, 2, 3, 4)
GROUP_MAX_COLORS = 80
GROUP_NAME_MAX_BYTES = 42
GROUP_MAX_TOLERANCE = 20.0
_NAME_XOR = 0x5A              # group names are stored XOR 0x5A
_GROUP_REGISTER, _GROUP_EMPTY = 2, 1
_DIFF_FORMULA_NAMES = {v: k for k, v in DIFF_FORMULAS.items()}


@dataclass
class ColorGroup:
    """A named palette stored in one of the device's 4 group slots (up to 80 L*a*b* colours), with the
    measuring settings and colour-difference tolerance the device uses when comparing against it."""
    slot: int
    name: str
    colors: list[Lab]
    illuminant: str = "D50"
    observer: int = 2
    m_factor: int = 0
    diff_formula: str = "DE2000"   # DE2000, DE94GA, DE94T, DE76
    tolerance: float = 1.0
    patch_border: bool = True      # the app always sends True
    hardware_id: int = 0           # 16-bit id the app uses to recognise its groups; 0 = pick a random one

    def to_dict(self) -> dict:
        return {"slot": self.slot, "name": self.name, "colors": [list(c) for c in self.colors],
                "illuminant": self.illuminant, "observer": self.observer, "m_factor": self.m_factor,
                "diff_formula": self.diff_formula, "tolerance": self.tolerance,
                "patch_border": self.patch_border, "hardware_id": self.hardware_id}

    @classmethod
    def from_dict(cls, d: dict) -> "ColorGroup":
        d = dict(d)
        d["colors"] = [Lab(*c[:3]) for c in d["colors"]]
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _truncate_utf8(text: str, max_bytes: int) -> bytes:
    out = b""
    for ch in text:  # cut on a character boundary, like the app
        b = ch.encode("utf-8")
        if len(out) + len(b) > max_bytes:
            break
        out += b
    return out


def encode_group_record(g: ColorGroup) -> bytes:
    """One group as it appears in SetGroupInformation (and in the GetGroupInformation reply)."""
    if g.slot not in GROUP_SLOTS:
        raise ValueError("slot must be 1..4")
    if not 1 <= len(g.colors) <= GROUP_MAX_COLORS:
        raise ValueError(f"a group needs 1..{GROUP_MAX_COLORS} colours")
    if not 0 <= g.hardware_id <= 0xFFFF:
        raise ValueError("hardware_id must fit in 16 bits")
    if g.observer not in (2, 10) or g.m_factor not in (0, 1, 2):
        raise ValueError("observer must be 2 or 10, m_factor 0..2")
    if not 0 <= g.tolerance <= GROUP_MAX_TOLERANCE:
        raise ValueError(f"tolerance must be 0..{GROUP_MAX_TOLERANCE}")
    name = _truncate_utf8(g.name, GROUP_NAME_MAX_BYTES)
    if not name:
        raise ValueError("group name must not be empty")
    illum = ILLUMINANTS[g.illuminant.upper()]
    formula = DIFF_FORMULAS[g.diff_formula.upper()]
    out = bytes([_GROUP_REGISTER, g.slot]) + struct.pack("<H", g.hardware_id) + bytes([len(name)])
    out += bytes(c ^ _NAME_XOR for c in name.ljust(GROUP_NAME_MAX_BYTES, b"\0"))
    out += bytes([1 if g.patch_border else 0, len(g.colors), g.m_factor, illum, g.observer, formula])
    out += struct.pack("<f", g.tolerance * 10.0)
    out += b"".join(struct.pack("<3f", *(v * 100.0 for v in c)) for c in g.colors)
    return out


def encode_set_groups(groups: Sequence[ColorGroup] = (), clear_slots: Sequence[int] = ()) -> bytes:
    """SetGroupInformation: store the given groups and/or clear the given slots, in one command."""
    body = b"".join(encode_group_record(g) for g in groups)
    for slot in clear_slots:
        if slot not in GROUP_SLOTS:
            raise ValueError("slot must be 1..4")
        body += bytes([_GROUP_EMPTY, slot])
    if not body:
        raise ValueError("nothing to send")
    return frame(Cmd.GROUP_SET, T_SET, body)


def encode_get_groups(size_check: bool = False) -> bytes:
    return frame(Cmd.GROUP_GET, T_SIZE if size_check else T_GET, b"\x01")


def parse_groups(payload: bytes) -> list[ColorGroup]:
    """Mirror of ResponseParser::ParseGetGroupInformation; empty slots are skipped."""
    out, i = [], 0
    while i + 2 <= len(payload):
        control, slot = payload[i], payload[i + 1]
        if control == _GROUP_EMPTY:
            i += 2
            continue
        if i + 0x39 > len(payload):
            raise ValueError("truncated group record")
        hw, = struct.unpack_from("<H", payload, i + 2)
        name_len = payload[i + 4]
        name = bytes(c ^ _NAME_XOR for c in payload[i + 5:i + 5 + GROUP_NAME_MAX_BYTES])[:name_len]
        border, n, mf, illum, angle, formula = payload[i + 0x2F:i + 0x35]
        tol, = struct.unpack_from("<f", payload, i + 0x35)
        start = i + 0x39
        if start + 12 * n > len(payload):
            raise ValueError("truncated group colours")
        colors = [Lab(*(v * 0.01 for v in struct.unpack_from("<3f", payload, start + 12 * k))) for k in range(n)]
        out.append(ColorGroup(slot, name.decode("utf-8", "replace"), colors, ILLUMINANT_NAMES.get(illum, "D50"),
                              angle, mf, _DIFF_FORMULA_NAMES.get(formula, "DE2000"), tol * 0.1, bool(border), hw))
        i = start + 12 * n
    return out


def parse_history_check(payload: bytes) -> tuple[int, int]:
    """Reply to encode_history_check: (after_id echoed back, number of records available after it)."""
    if len(payload) < 8:
        raise ValueError(f"short history-check reply ({len(payload)} bytes)")
    return struct.unpack_from("<II", payload)


def parse_history(payload: bytes) -> list[MeasurementResult]:
    """Reply to encode_history_get: back-to-back 143-byte records."""
    if len(payload) % HISTORY_RECORD_LEN:
        raise ValueError(f"history payload {len(payload)} bytes is not a multiple of {HISTORY_RECORD_LEN}")
    return [parse_result_record(payload[i:i + HISTORY_RECORD_LEN], from_history=True)
            for i in range(0, len(payload), HISTORY_RECORD_LEN)]


def parse_result_cache(payload: bytes) -> list[MeasurementResult]:
    if len(payload) % RESULT_RECORD_LEN:
        raise ValueError(f"cache payload {len(payload)} bytes is not a multiple of {RESULT_RECORD_LEN}")
    return [parse_result_record(payload[i:i + RESULT_RECORD_LEN]) for i in range(0, len(payload), RESULT_RECORD_LEN)]
