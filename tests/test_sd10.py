"""Offline tests. Byte vectors come from libmunsell-lib.so run under emulation and from a real SD-10."""
import asyncio
import datetime as dt
import struct

import pytest

import sd10
from sd10 import protocol as p

H = bytes.fromhex


# ---- command encoding (native CommandBuilder output) --------------------------------------------
def test_simple_commands():
    assert p.frame(p.Cmd.STATUS, p.T_GET, b"\0") == H("00430b110100000000")
    assert p.frame(p.Cmd.MEASURE, p.T_EXEC, b"\1") == H("004301120100000001")


def test_control_device():
    app_id = bytes(range(0xA0, 0xB0))
    assert p.encode_control_device(True, app_id) == H("004319121800000001010102010103 10") + app_id
    assert p.encode_control_device(False, app_id, 2) == H("004319121800000001010202010203 10") + app_id


def test_datetime():
    assert p.encode_datetime(dt.datetime(2026, 10, 17, 13, 45, 30)) == H("00430d1306000000 1a0a110d2d1e")
    with pytest.raises(ValueError):
        p.encode_datetime(dt.datetime(2019, 1, 1))


def test_measurement_modes():
    assert p.encode_measurement_mode(p.Method.SPOT_SINGLE, True, 1, 1, 10) == \
        H("00431a130f000000 010101 020102 040101 050101 06010a")
    assert p.encode_measurement_mode(p.Method.SPOT_COMPARISON, True, 0, 0, 2, 3, 1.5) == \
        H("00431a1318000000 010102 020102 040100 050100 060102 070103 080400007041")
    assert p.encode_measurement_mode(p.Method.SCAN, False, 0, 0, 2, patch_num=2,
                                     labs=[(50, 10, -20), (75.5, -3.25, 4)], line_no=1) == \
        H("00431a1333000000 010105 020101 040100 050100 060102 0a0102 0d0100"
          "0e18 00409c45 00007a44 0000fac4 00f0eb45 0080a2c3 0000c843 0f020100")
    assert p.encode_measurement_mode(p.Method.CALIBRATION) == H("00431a1303000000 010104")


# ---- reply parsing ------------------------------------------------------------------------------
def test_status_from_real_device():
    st = p.parse_status(H("010104 02080000000000000000 030102 040163 050100 060100"))
    assert (st.idle, st.calibrated, st.battery, st.errors) == (True, True, 99, [])
    assert p.parse_status(H("010100 02020102")).error_status == 0x0102  # big-endian bitmask


def _record():
    rec = bytearray(144)
    rec[0:7] = bytes([1, 26, 10, 17, 13, 45, 30])
    for i in range(31):
        struct.pack_into("<f", rec, 7 + 4 * i, 0.18)
    rec[0x83:0x87] = bytes([0, 0, 2, 0])
    rec[0x87:0x8B] = bytes([0xDE, 0xAD, 0xBE, 0xEF])
    struct.pack_into("<H", rec, 0x8B, 0x1234)
    rec[0x8D] = 7
    struct.pack_into("<H", rec, 0x8E, 0x0102)
    return bytes(rec)


def test_result_record():
    r = p.parse_result_record(_record())
    assert r.ok and r.timestamp == dt.datetime(2026, 10, 17, 13, 45, 30)
    assert (r.identifier, r.inspection_no, r.patch_no, r.group_id) == (0xEFBEADDE, 0x1234, 7, 0x0102)
    assert (r.illuminant, r.observer) == ("D50", 2)


# ---- colour (values from librmcc.so under emulation) --------------------------------------------
def test_lab_matches_native():
    grey = sd10.spectrum_to_lab([0.18] * 31, "D50", 2)
    assert grey == pytest.approx((49.488325787284005, -0.078772844850028, 0.024830189680002945), abs=1e-5)
    white = sd10.spectrum_to_lab([1.0] * 31)
    assert white == pytest.approx((99.9862176321324, -0.13951439764997575, 0.04397668972000446), abs=1e-5)


@pytest.mark.parametrize("l1,l2,de", [  # Sharma, Wu & Dalal (2005) test data
    ((50, 2.6772, -79.7751), (50, 0, -82.7485), 2.0425),
    ((50, 0, 0), (50, -1, 2), 2.3669),
    ((50, 2.5, 0), (73, 25, -18), 27.1492),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
])
def test_ciede2000(l1, l2, de):
    assert sd10.delta_e_2000(l1, l2) == pytest.approx(de, abs=1e-4)


# ---- transport, against a scripted fake device --------------------------------------------------
class FakeBle:
    """Answers like an SD-10; sends an event byte in front of one reply, as the real device may."""
    address = "00:11:22:33:44:55"
    is_connected = True
    mtu_size = 23

    def __init__(self):
        self.writes, self.cb, self.buf = [], None, b""
        self.cover_closed = False
        self.calibration = 2       # 0 = calibration needed, 2 = done

    async def start_notify(self, uuid, cb):
        self.cb = cb

    def _notify(self, data):
        for i in range(0, len(data), 20):
            asyncio.get_running_loop().call_soon(self.cb, None, bytearray(data[i:i + 20]))

    async def write_gatt_char(self, uuid, data, response=True):
        self.writes.append(bytes(data))
        if data in (b"\xff", b"\xfe"):
            return
        self.buf += data
        if len(self.buf) < 8 or len(self.buf) < 8 + struct.unpack_from("<I", self.buf, 4)[0]:
            return
        cid, typ = self.buf[2], self.buf[3]
        if cid == p.Cmd.MODE_SET:
            self.mode = p.parse_tlv(self.buf[8:])
        self.buf = b""
        if cid == p.Cmd.CONTROL or cid == p.Cmd.MODE_SET:
            self._notify(p.frame(cid, 1, b"OK"))
        elif cid == p.Cmd.STATUS:  # real device reply (idle, 99 %), calibration/shutter as configured
            shutter = b"\x01" if self.cover_closed else b"\x00"
            self._notify(p.frame(cid, 1, H("010104 02080000000000000000 0301") + bytes([self.calibration])
                                 + H("040163 0501") + shutter + H("060100")))
        elif cid in (p.Cmd.NOTIFY_RESULT, p.Cmd.NOTIFY_GROUP_DONE, p.Cmd.STANDALONE):
            self._notify(p.frame(cid, 1, b"OK"))
        elif cid == p.Cmd.CALIBRATE:
            self._notify(p.frame(cid, 1, b"OK"))
            asyncio.get_running_loop().call_later(0.05, self._calibrated)
        elif cid == p.Cmd.MEASURE:
            self._notify(p.frame(cid, 1, b"OK"))
            asyncio.get_running_loop().call_later(0.05, self.cb, None, bytearray([p.Event.FINISH_MEASURING]))
        elif cid == p.Cmd.RESULT_CACHE and typ == p.T_SIZE:
            self._notify(bytes([p.Event.CHANGED_STATUS]) + p.frame(cid, 0, struct.pack("<I", 144)))
        elif cid == p.Cmd.RESULT_CACHE:
            self._notify(p.frame(cid, 1, b"".join(self.cache_records())))

    def cache_records(self):
        return [_record()]

    def _calibrated(self):
        self.calibration = 2
        self.cb(None, bytearray([p.Event.FINISH_CALIBRATION]))


def test_calibration_and_readiness_checks():
    async def run():
        ble = FakeBle()
        dev = sd10.SD10(ble, app_id=bytes(16))
        await dev.start()
        out = []
        ble.calibration = 0
        try:
            await dev.measure(timeout=2)
        except sd10.CalibrationNeeded:
            out.append("needs calibration")
        try:
            await dev.calibrate(timeout=2)            # cover open -> refused
        except sd10.CoverOpen:
            out.append("cover open")
        ble.cover_closed = True
        st = await dev.calibrate(timeout=2)
        out.append(("calibrated", st.calibrated))
        ble.cover_closed = False
        out.append(("measured", (await dev.measure(timeout=2)).ok))
        return out, ble
    out, ble = asyncio.run(run())
    assert out == ["needs calibration", "cover open", ("calibrated", True), ("measured", True)]
    assert sum(1 for w in ble.writes if w[:3] == bytes([0, 0x43, p.Cmd.CALIBRATE])) == 1


def test_connect_retries_cancelled_notify(monkeypatch):
    """Windows sometimes cancels the CCCD write right after connecting; connect() must retry."""
    import sd10.client as c
    made = []

    class FlakyBle(FakeBle):
        def __init__(self, target):
            super().__init__()
            made.append(self)

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def start_notify(self, uuid, cb):
            if len(made) == 1:
                raise OSError(-2147023673, "The operation was canceled by the user.")
            await super().start_notify(uuid, cb)

    async def fake_find(address, timeout):
        return type("D", (), {"address": "00:11:22:33:44:55", "name": "SD-10"})()

    monkeypatch.setattr(c, "BleakClient", FlakyBle)
    monkeypatch.setattr(c, "find_device", fake_find)

    async def run():
        async with c.connect(app_id=bytes(16), retry_delay=0) as dev:
            return dev.session_open
    assert asyncio.run(run()) is True
    assert len(made) == 2


@pytest.mark.parametrize("mode", ["ctrl_c", "break"])
def test_sync_button_loop_stops_cleanly(mode):
    """Stopping dev.button_measurements() with Ctrl+C or break must not leave pending tasks behind.

    Runs in a fresh interpreter because the symptoms ("Task was destroyed but it is pending",
    "Event loop is closed") are printed at interpreter shutdown, where pytest can't see them.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path
    here = Path(__file__).resolve().parent
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(here.parent), str(here)]))
    proc = subprocess.run([sys.executable, str(here / "_button_scenario.py"), mode],
                          capture_output=True, text=True, timeout=60, env=env)
    assert "results=" in proc.stdout and "results=0" not in proc.stdout, proc.stdout + proc.stderr
    for bad in ("Task was destroyed", "Event loop is closed", "Exception ignored"):
        assert bad not in proc.stderr, proc.stderr

def test_button_press_makes_host_run_measurement():
    """In a session the device only reports MEASURE_BUTTON_PUSH (C2); the host must run the measurement."""
    async def run():
        ble = FakeBle()
        dev = sd10.SD10(ble, app_id=bytes(16))
        await dev.start()
        await dev.open_session()
        await dev.measure(illuminant="D65", observer=10)          # remembered for button presses
        loop = asyncio.get_running_loop()
        for delay in (0.05, 0.1, 2.0):                            # 2nd press is a bounce (<1.5 s)
            loop.call_later(delay, ble.cb, None, bytearray([p.Event.MEASURE_BUTTON_PUSH]))
        agen = dev.button_measurements()
        results = [await asyncio.wait_for(agen.__anext__(), 5) for _ in range(2)]
        await agen.aclose()
        return ble, results
    ble, results = asyncio.run(run())
    measures = [w for w in ble.writes if w[:4] == bytes([0, 0x43, p.Cmd.MEASURE, p.T_EXEC])]
    modes = [w for w in ble.writes if w[:3] == bytes([0, 0x43, p.Cmd.MODE_SET])]
    assert len(results) == 2 and len(measures) == 3               # initial measure + 2 presses (bounce ignored)
    expected = p.encode_measurement_mode(p.Method.SPOT_SINGLE, True, 0, 1, 10)  # D65 / 10 deg reused
    assert modes[-1] == expected[:20]                             # writes are split into 20-byte chunks


def test_measure_refuses_when_cover_closed():
    """With the cover closed the real SD-10 answers OK + C1 but re-sends its previous result; refuse instead."""
    async def run():
        ble = FakeBle()
        ble.cover_closed = True
        dev = sd10.SD10(ble, app_id=bytes(16))
        await dev.start()
        await dev.open_session()
        with pytest.raises(sd10.CoverClosed):
            await dev.measure(timeout=2)
        # button presses with the cover closed are reported and skipped, not fatal
        ble.cover_closed = True
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, ble.cb, None, bytearray([p.Event.MEASURE_BUTTON_PUSH]))
        loop.call_later(0.5, lambda: setattr(ble, "cover_closed", False))
        loop.call_later(1.7, ble.cb, None, bytearray([p.Event.MEASURE_BUTTON_PUSH]))
        agen = dev.button_measurements()
        result = await asyncio.wait_for(agen.__anext__(), 5)
        await agen.aclose()
        return ble, result
    ble, result = asyncio.run(run())
    measures = [w for w in ble.writes if w[:4] == bytes([0, 0x43, p.Cmd.MEASURE, p.T_EXEC])]
    assert len(measures) == 1          # only the press with the cover open actually measured
    assert result.ok


def test_history_encoding():  # native CommandBuilder output
    assert p.encode_history_check(0x01020304) == H("0043131105000000 01 04030201")
    assert p.encode_history_get(0x01020304, 2) == H("0043141209000000 01 04030201 02000000")
    assert p.parse_history_check(H("b9030000 64000000")) == (953, 100)
    recs = p.parse_history(_record()[1:] * 2)
    assert len(recs) == 2 and recs[0].identifier == 0xEFBEADDE and recs[0].ok


def test_group_encoding():  # native CreateSetGroupInfomation output
    g = p.ColorGroup(slot=4, name="Test", colors=[sd10.Lab(1, 2, 3)], illuminant="F11", observer=10, m_factor=2,
                     diff_formula="DE76", tolerance=1.5, patch_border=True, hardware_id=0x1234)
    assert p.encode_set_groups([g]) == (H("0043151345000000 02 04 3412 04 0e3f292e") + b"\x5a" * 38 +
                                        H("01 01 02 1b 0a 03 00007041 0000c842 00004843 00009643"))
    assert p.encode_set_groups(clear_slots=[3]) == H("00431513020000000103")
    back = p.parse_groups(p.encode_set_groups([g])[8:] + H("0102"))
    assert back == [g]
    with pytest.raises(ValueError):
        p.encode_group_record(sd10.ColorGroup(slot=5, name="x", colors=[sd10.Lab(1, 2, 3)]))
    with pytest.raises(ValueError):
        p.encode_group_record(sd10.ColorGroup(slot=1, name="x", colors=[]))
    assert p._truncate_utf8("ü" * 30, 42) == "ü".encode() * 21  # cut on a character boundary


class GroupBle(FakeBle):
    """Four group slots, read and written with the real wire format."""
    def __init__(self):
        super().__init__()
        self.slots = {}

    async def write_gatt_char(self, uuid, data, response=True):
        data = bytes(data)
        if data[:3] == bytes([0, 0x43, p.Cmd.GROUP_GET]):
            self.writes.append(data)
            if data[3] == p.T_SIZE:
                self._notify(p.frame(p.Cmd.GROUP_GET, 0, struct.pack("<I", 0)))
            else:
                body = b"".join(self.slots.get(s, bytes([1, s])) for s in (1, 2, 3, 4))
                self._notify(p.frame(p.Cmd.GROUP_GET, 1, body))
            return
        if data[:3] == bytes([0, 0x43, p.Cmd.GROUP_SET]) or self.buf[:3] == bytes([0, 0x43, p.Cmd.GROUP_SET]):
            self.writes.append(data)
            self.buf += data   # the command arrives in 20-byte chunks
            n, = struct.unpack_from("<I", self.buf, 4)
            if len(self.buf) < 8 + n:
                return
            body, self.buf = self.buf[8:8 + n], b""
            for g in p.parse_groups(body):
                self.slots[g.slot] = p.encode_group_record(g)
            i = 0
            while i < len(body):
                if body[i] == 1:
                    self.slots.pop(body[i + 1], None)
                    i += 2
                else:
                    i += 0x39 + 12 * body[i + 0x30]
            self._notify(p.frame(p.Cmd.GROUP_SET, 1, b"OK"))
            return
        await super().write_gatt_char(uuid, data, response)


def test_group_management_flow():
    async def run():
        ble = GroupBle()
        dev = sd10.SD10(ble, app_id=bytes(16))
        await dev.start()
        assert await dev.get_groups() == []
        stored = await dev.set_group(sd10.ColorGroup(2, "Wall paint", [sd10.Lab(50, 1, 2), sd10.Lab(60, -3, 4)],
                                                     illuminant="D65", observer=10, tolerance=2.0))
        await dev.set_group(sd10.ColorGroup(4, "Logo", [sd10.Lab(30, 40, -50)]))
        groups = await dev.get_groups()
        await dev.delete_group(4)
        after = await dev.get_groups()
        return stored, groups, after
    stored, groups, after = asyncio.run(run())
    assert 1 <= stored.hardware_id <= 65534
    assert [(g.slot, g.name, len(g.colors)) for g in groups] == [(2, "Wall paint", 2), (4, "Logo", 1)]
    assert groups[0].illuminant == "D65" and groups[0].observer == 10 and groups[0].tolerance == pytest.approx(2.0)
    assert groups[0].colors[1] == pytest.approx((60, -3, 4))
    assert [g.slot for g in after] == [2]


def test_add_and_remove_group_colours():
    async def run():
        ble = GroupBle()
        dev = sd10.SD10(ble, app_id=bytes(16))
        await dev.start()
        orig = await dev.set_group(sd10.ColorGroup(3, "Paint", [sd10.Lab(50, 1, 2)], illuminant="D65", observer=10,
                                                   diff_formula="DE76", tolerance=2.0))
        reading = p.parse_result_record(_record())          # 0.18 grey spectrum, recorded under D50/2
        added = await dev.add_to_group(3, [(60, -3, 4), reading])
        removed = await dev.remove_from_group(3, [0])
        errors = []
        for call in (lambda: dev.add_to_group(1, [(1, 2, 3)]),                       # empty slot
                     lambda: dev.add_to_group(3, [(1, 2, 3)] * 79),                  # > 80 colours
                     lambda: dev.remove_from_group(3, [0, 1])):                      # would leave none
            try:
                await call()
            except (sd10.SD10Error, ValueError) as ex:
                errors.append(type(ex).__name__)
        return orig, added, removed, errors, await dev.get_groups()
    orig, added, removed, errors, final = asyncio.run(run())
    assert len(added.colors) == 3 and added.colors[1] == pytest.approx((60, -3, 4))
    # the reading is converted under the palette's D65/10, not the D50/2 it was measured with
    assert added.colors[2] == pytest.approx(sd10.spectrum_to_lab([0.18] * 31, "D65", 10), abs=1e-4)
    assert (added.name, added.illuminant, added.observer, added.diff_formula, added.hardware_id) == \
        (orig.name, orig.illuminant, orig.observer, orig.diff_formula, orig.hardware_id)
    assert len(removed.colors) == 2 and removed.colors[0] == pytest.approx((60, -3, 4))
    assert errors == ["SD10Error", "ValueError", "ValueError"]
    assert final == [removed]


def _grey_record(level):
    rec = bytearray(_record())
    for i in range(31):
        struct.pack_into("<f", rec, 7 + 4 * i, level)
    return bytes(rec)


class ScanBle(GroupBle):
    """Returns one record per patch for scan modes; patch k is a grey of reflectance greys[k]."""
    def __init__(self, greys):
        super().__init__()
        self.greys = greys
        self.mode = {}

    def cache_records(self):
        n = self.mode.get(0x0A, b"\x01")[0]
        start = self.mode.get(0x0B, b"\x01")[0]
        return [_grey_record(self.greys[(start - 1 + k) % len(self.greys)]) for k in range(n)]


def _grey_lab(g, ill="D50", obs=2):
    return sd10.spectrum_to_lab([g] * 31, ill, obs)


def test_scan_row_plain_and_compared():
    greys = [0.1, 0.3, 0.5, 0.7]
    async def run():
        ble = ScanBle(greys)
        dev = sd10.SD10(ble, app_id=bytes(16))
        await dev.start()
        plain = await dev.scan_row(patches=4, wait_for_button=False, timeout=2)
        mode_plain = dict(ble.mode)
        expected = [_grey_lab(g) for g in greys]
        expected[2] = sd10.Lab(expected[2].L + 3, expected[2].a, expected[2].b)   # one patch is off by dL 3
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, ble.cb, None, bytearray([p.Event.MEASURE_BUTTON_PUSH]))  # user presses at row start
        compared = await dev.scan_row(expected, tolerance=1.0, formula="DE76", row=2, timeout=2)
        return ble, plain, mode_plain, compared
    ble, plain, mode_plain, compared = asyncio.run(run())
    assert mode_plain[0x01] == b"\x05" and mode_plain[0x0A] == b"\x04"         # SCAN, 4 patches
    assert [round(x.lab.L, 3) for x in plain.patches] == [round(_grey_lab(g).L, 3) for g in greys]
    assert plain.passed is None and plain.patches[0].delta_e is None
    assert ble.mode[0x01] == b"\x03" and ble.mode[0x07] == b"\x03" and ble.mode[0x0D] == b"\x01"  # SCAN_COMPARISON
    assert struct.unpack("<H", ble.mode[0x0F])[0] == 2
    assert [x.ok for x in compared.patches] == [True, True, False, True]
    assert compared.patches[2].delta_e == pytest.approx(3.0, abs=1e-6)
    assert compared.mean_delta_e == pytest.approx(0.75, abs=1e-6) and compared.passed is True  # app rule: mean <= tol
    notes = [w[8] for w in ble.writes if w[:3] == bytes([0, 0x43, p.Cmd.NOTIFY_RESULT])]
    assert notes == [1, 1]
    with pytest.raises(ValueError):
        asyncio.run(sd10.SD10(ScanBle(greys), app_id=bytes(16)).scan_row(patches=19, wait_for_button=False))


def test_scan_palette_rows_and_spot_patch():
    greys = [0.1 + 0.03 * k for k in range(23)]                   # palette of 23 colours -> rows of 10, 10, 3
    async def run():
        ble = ScanBle(greys)
        dev = sd10.SD10(ble, app_id=bytes(16))
        await dev.start()
        colours = [_grey_lab(g, "D65", 10) for g in greys]
        colours[12] = sd10.Lab(colours[12].L - 5, colours[12].a, colours[12].b)  # patch 13 printed wrong
        await dev.set_group(sd10.ColorGroup(3, "Chart", colours, illuminant="D65", observer=10,
                                            diff_formula="DE2000", tolerance=1.0))
        loop = asyncio.get_running_loop()
        for t in (0.05, 0.3, 0.6):
            loop.call_later(t, ble.cb, None, bytearray([p.Event.MEASURE_BUTTON_PUSH]))
        rows = [r async for r in dev.scan_palette(3, timeout=2)]
        modes = ble.mode
        spot = await dev.measure_palette_patch(3, 13, timeout=2)
        return ble, rows, spot
    ble, rows, spot = asyncio.run(run())
    assert [(r.row, len(r.patches), r.patches[0].patch) for r in rows] == [(1, 10, 1), (2, 10, 11), (3, 3, 21)]
    assert all(r.illuminant == "D65" and r.observer == 10 and r.formula == "DE2000" for r in rows)
    assert [x.patch for x in rows[1].failed_patches] == [13]
    assert rows[1].passed is True and rows[0].passed is True     # one bad patch, but mean dE <= 1
    assert spot.patch == 13 and spot.ok is False and spot.delta_e > 3
    assert any(w[:3] == bytes([0, 0x43, p.Cmd.NOTIFY_GROUP_DONE]) for w in ble.writes)
    assert ble.mode[0x01] == b"\x02" and ble.mode[0x09] == b"\x03" and ble.mode[0x0B] == bytes([13])  # SPOT_GROUP


def test_color_helpers_match_native():
    # values from libmunsell-lib.so (EPMColorDifference / EPMHarmonicLab / EPMLab2sRGB) under emulation
    a, b = (50, 2.6772, -79.7751), (50, 0, -82.7485)
    assert sd10.delta_e(a, b, "DE94GA") == pytest.approx(1.395, abs=1e-3)
    assert sd10.delta_e(a, b, "DE94T") == pytest.approx(1.423, abs=1e-3)
    assert sd10.delta_e(a, b, "DE2000") == pytest.approx(2.0425, abs=1e-4)
    assert sd10.color_scheme((50, 40, 20), "complementary")[0] == pytest.approx((69.25, -29.61, -9.54), abs=0.01)
    assert len(sd10.color_scheme((50, 40, 20), "tetradic")) == 3
    assert sd10.lab_to_hex((50, 40, 20)) == "#B95857"
    assert sd10.median_lab([(1, 40, 100), (3, 10, 300), (2, 30, 200), (10, 20, 50)]) == (2.5, 25.0, 150.0)


def test_aco_round_trip(tmp_path):
    path = sd10.export_aco(tmp_path / "x.aco", [(50.0, 10.25, -20.5), (100.0, -128.0, 127.0)], ["Wall", "Logo ü"])
    assert sd10.read_aco(path) == [("Wall", (50.0, 10.25, -20.5)), ("Logo ü", (100.0, -128.0, 127.0))]
    data = path.read_bytes()
    assert data[:4] == b"\x00\x01\x00\x02" and data[4:6] == b"\x00\x07"   # version 1, 2 colours, Lab space


class HistoryBle(FakeBle):
    """Stores readings 1..N; answers history queries the way the real SD-10 does (<= 100 per answer)."""
    def __init__(self, n):
        super().__init__()
        self.ids = list(range(1, n + 1))
        self.batches = []

    def _rec(self, ident):
        rec = bytearray(_record()[1:])
        struct.pack_into("<I", rec, 0x86, ident)
        return bytes(rec)

    async def write_gatt_char(self, uuid, data, response=True):
        data = bytes(data)
        if data[:3] == bytes([0, 0x43, p.Cmd.HISTORY_OFFSET]):
            self.writes.append(data)
            after, = struct.unpack_from("<I", data, 9)
            avail = min(100, len([i for i in self.ids if i > after]))
            self._notify(p.frame(p.Cmd.HISTORY_OFFSET, 1, struct.pack("<II", after, avail)))
            return
        if data[:3] == bytes([0, 0x43, p.Cmd.HISTORY]):
            self.writes.append(data)
            after, n = struct.unpack_from("<II", data, 9)
            sel = [i for i in self.ids if i > after][:n]
            self.batches.append(len(sel))
            self._notify(p.frame(p.Cmd.HISTORY, 1, b"".join(self._rec(i) for i in sel)))
            return
        await super().write_gatt_char(uuid, data, response)


def test_get_history_batches_and_after():
    async def run():
        ble = HistoryBle(230)
        dev = sd10.SD10(ble, app_id=bytes(16))
        await dev.start()
        seen = []
        everything = await dev.get_history(progress=seen.append)
        newer = await dev.get_history(after_id=225)
        limited = await dev.get_history(limit=7)
        empty = await dev.get_history(after_id=230)
        return ble, everything, newer, limited, empty, seen
    ble, everything, newer, limited, empty, seen = asyncio.run(run())
    assert [r.identifier for r in everything] == list(range(1, 231))
    assert ble.batches[:3] == [100, 100, 30] and seen == [100, 200, 230]
    assert [r.identifier for r in newer] == [226, 227, 228, 229, 230]
    assert [r.identifier for r in limited] == list(range(1, 8))
    assert empty == []


def test_measure_flow():
    async def run():
        ble = FakeBle()
        dev = sd10.SD10(ble, app_id=bytes(16))
        await dev.start()
        await dev.open_session()
        result = await dev.measure(timeout=2)
        return ble, dev, result
    ble, dev, result = asyncio.run(run())
    assert result.lab == pytest.approx((49.4883, -0.0788, 0.0248), abs=1e-3)
    assert ble.writes.count(b"\xff") == 6           # every reply ACKed: open, status, mode, measure, size, cache
    assert dev.events.get_nowait() == p.Event.CHANGED_STATUS  # event in front of a reply was split off
