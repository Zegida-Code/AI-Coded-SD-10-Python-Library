"""Command-line interface: ``python -m sd10 ...`` or ``sd10 ...`` once installed."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict

from . import protocol as p
from .client import SD10, SD10Error, connect, scan
from .color import ILLUMINANTS, WAVELENGTHS
from .protocol import MeasurementResult, Setting


def _print_results(results: list[MeasurementResult], args) -> None:
    ill = getattr(args, "illuminant", None)
    obs = getattr(args, "observer", None)
    if args.json:
        rows = []
        for r in results:
            d = r.to_dict()
            if ill or obs:
                lab = r.lab_under(ill or r.illuminant, obs or r.observer)
                d.update(illuminant=ill or r.illuminant, observer=obs or r.observer,
                         lab={"L": lab.L, "a": lab.a, "b": lab.b}, lch=dict(zip("LCh", lab.lch)))
            rows.append(d)
        print(json.dumps(rows, indent=2))
        return
    for r in results:
        i, o = ill or r.illuminant, obs or r.observer
        lab = r.lab_under(i, o)
        lch = lab.lch
        when = r.timestamp.isoformat() if r.timestamp else "?"
        print(f"L*={lab.L:7.2f}  a*={lab.a:7.2f}  b*={lab.b:7.2f}    (C*={lch.C:.2f}  h={lch.h:.1f})   "
              f"{i}/{o}deg  M{r.m_factor}  {when}" + ("" if r.ok else "  [device flagged failure]"))
        if args.spectrum:
            for nm, v in zip(WAVELENGTHS, r.spectrum):
                print(f"  {nm} nm  {v:.5f}")


async def _run(args) -> None:
    if args.cmd == "scan":
        found = await scan(args.timeout)
        if not found:
            print("no devices advertising", p.ADV_SERVICE_UUID)
        for f in found:
            weak = "  (weak signal; the app ignores < -75 dBm)" if f.rssi < -75 else ""
            print(f"{f.address}  {f.rssi:4d} dBm  {f.name}{weak}")
        return

    async with connect(args.address, scan_timeout=args.timeout) as dev:
        await _COMMANDS[args.cmd](dev, args)


async def _info(dev: SD10, args) -> None:
    info = await dev.get_info()
    out = {"name": info.name, "firmware": info.firmware, "serial": info.serial, "status": asdict(info.status),
           "settings": {s.name.lower(): await dev.get_setting(s) for s in Setting},
           "measurement_mode_tlv": {f"0x{k:02X}": v.hex() for k, v in (await dev.get_measurement_mode()).items()}}
    print(json.dumps(out, indent=2))


async def _measure(dev: SD10, args) -> None:
    r = await dev.measure(illuminant=args.illuminant, observer=args.observer, m_factor=args.m,
                          save=not args.no_save, timeout=args.wait)
    _print_results([r], args)


async def _calibrate(dev: SD10, args) -> None:
    st = await dev.calibrate(timeout=args.wait)
    print("calibration finished; calibrated =", st.calibrated, st.errors or "")


async def _listen(dev: SD10, args) -> None:
    print("listening; press the measure button on the device (Ctrl+C to stop)", file=sys.stderr)
    async for r in dev.button_measurements():
        _print_results([r], args)


async def _set_time(dev: SD10, args) -> None:
    await dev.set_clock()
    print("clock set")


async def _set_name(dev: SD10, args) -> None:
    await dev.set_name(args.name)
    print("name set to", await dev.get_name())


async def _raw(dev: SD10, args) -> None:
    cid, typ, payload = await dev.transact(p.frame(int(args.id, 16), int(args.type, 16), bytes.fromhex(args.payload)))
    print(f"id=0x{cid:02X} type=0x{typ:02X} len={len(payload)} payload={payload.hex(' ')}")
    try:
        print("  ascii:", payload.decode("ascii"))
    except UnicodeDecodeError:
        pass


_COMMANDS = {"info": _info, "measure": _measure, "calibrate": _calibrate, "listen": _listen,
             "set-time": _set_time, "set-name": _set_name, "raw": _raw}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="sd10", description="Epson SD-10 spectrophotometer over Bluetooth LE")
    ap.add_argument("-a", "--address", help="BLE address (default: strongest SD-10 found)")
    ap.add_argument("-t", "--timeout", type=float, default=8.0, help="scan timeout (s)")
    ap.add_argument("-v", "--verbose", action="store_true", help="hex-dump all traffic to stderr")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="list nearby SD-10s")
    sub.add_parser("info", help="status, firmware, serial, settings")
    m = sub.add_parser("measure", help="single spot measurement -> L*a*b*")
    m.add_argument("--m", type=int, choices=(0, 1, 2), default=0, help="M-factor (M0/M1/M2)")
    m.add_argument("--illuminant", choices=sorted(ILLUMINANTS), default="D50")
    m.add_argument("--observer", type=int, choices=(2, 10), default=2)
    m.add_argument("--no-save", action="store_true", help="don't store the result in device history")
    m.add_argument("--wait", type=float, default=20.0, help="seconds to wait for the result")
    m.add_argument("--spectrum", action="store_true", help="also print the 31-band reflectance")
    m.add_argument("--json", action="store_true")
    c = sub.add_parser("calibrate", help="run white calibration")
    c.add_argument("--wait", type=float, default=30.0)
    lst = sub.add_parser("listen", help="show L*a*b* for measurements taken with the device button")
    lst.add_argument("--illuminant", choices=sorted(ILLUMINANTS), help="default: as recorded by the device")
    lst.add_argument("--observer", type=int, choices=(2, 10))
    lst.add_argument("--spectrum", action="store_true")
    lst.add_argument("--json", action="store_true")
    sub.add_parser("set-time", help="set the device clock to local time")
    n = sub.add_parser("set-name", help="rename the device (max 16 bytes)")
    n.add_argument("name")
    r = sub.add_parser("raw", help="send a raw framed command")
    r.add_argument("id", help="command id, hex (e.g. 0b)")
    r.add_argument("type", help="type, hex (10/11/12/13)")
    r.add_argument("payload", nargs="?", default="", help="payload hex")
    args = ap.parse_args(argv)

    logging.basicConfig(format="  [%(name)s] %(message)s", stream=sys.stderr)
    logging.getLogger("sd10").setLevel(logging.DEBUG if args.verbose else logging.WARNING)
    try:
        asyncio.run(_run(args))
    except SD10Error as ex:
        sys.exit(f"error: {ex}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
