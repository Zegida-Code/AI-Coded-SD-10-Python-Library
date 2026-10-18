"""Command-line interface: ``python -m pysd10 ...`` or ``pysd10 ...`` once installed."""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
from dataclasses import asdict

from . import protocol as p
from .client import SD10, SD10Error, connect, scan
from .color import ILLUMINANTS, WAVELENGTHS
from .export import export_aco
from .protocol import ColorGroup, MeasurementResult, Setting


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
    async for r in dev.button_measurements(illuminant=args.illuminant, observer=args.observer):
        _print_results([r], args)


async def _history(dev: SD10, args) -> None:
    results = await dev.get_history(after_id=args.after,
                                    progress=lambda n: print(f"  downloaded {n}...", file=sys.stderr))
    if args.last:
        results = results[-args.last:]
    if args.aco:
        export_aco(args.aco, [r.lab for r in results], [f"#{r.identifier}" for r in results])
        print(f"wrote {len(results)} swatches to {args.aco}", file=sys.stderr)
    elif args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["id", "timestamp", "illuminant", "observer", "m_factor", "L", "a", "b", "C", "h"]
                       + [f"{nm}nm" for nm in WAVELENGTHS])
            for r in results:
                lab = r.lab
                w.writerow([r.identifier, r.timestamp.isoformat() if r.timestamp else "", r.illuminant, r.observer,
                            f"M{r.m_factor}", *(f"{v:.4f}" for v in (*lab, lab.lch.C, lab.lch.h))]
                           + [f"{v:.6f}" for v in r.spectrum])
        print(f"wrote {len(results)} readings to {args.csv}", file=sys.stderr)
    elif args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        for r in results:
            lab = r.lab
            when = r.timestamp.isoformat(sep=" ") if r.timestamp else "?"
            print(f"#{r.identifier:<6} {when}  L*={lab.L:7.2f}  a*={lab.a:7.2f}  b*={lab.b:7.2f}   "
                  f"{r.illuminant}/{r.observer}deg  M{r.m_factor}")
        print(f"{len(results)} readings", file=sys.stderr)


def _print_group(g: ColorGroup) -> None:
    print(f"slot {g.slot}: {g.name!r}  {len(g.colors)} colours  {g.illuminant}/{g.observer}deg M{g.m_factor}  "
          f"{g.diff_formula} tolerance {g.tolerance:.2f}")
    for i, c in enumerate(g.colors, 1):
        print(f"    {i:2}  L*={c.L:7.2f}  a*={c.a:7.2f}  b*={c.b:7.2f}")


async def _groups(dev: SD10, args) -> None:
    action = args.action
    if action == "list":
        groups = await dev.get_groups()
        if args.json:
            print(json.dumps([g.to_dict() for g in groups], indent=2, ensure_ascii=False))
        elif not groups:
            print("no groups stored (slots 1-4 are empty)")
        for g in groups if not args.json else ():
            _print_group(g)
    elif action == "delete":
        await dev.delete_group(args.slot)
        print(f"slot {args.slot} cleared")
    elif action == "export":
        match = [g for g in await dev.get_groups() if g.slot == args.slot]
        if not match:
            raise SD10Error(f"slot {args.slot} is empty")
        if args.file.lower().endswith(".aco"):
            g = match[0]
            export_aco(args.file, g.colors, [f"{g.name} {i}" for i in range(1, len(g.colors) + 1)])
        else:
            with open(args.file, "w", encoding="utf-8") as f:
                json.dump(match[0].to_dict(), f, indent=2, ensure_ascii=False)
        print(f"wrote slot {args.slot} to {args.file}")
    elif action == "import":
        with open(args.file, encoding="utf-8") as f:
            g = ColorGroup.from_dict(json.load(f))
        if args.slot:
            g.slot = args.slot
        _print_group(await dev.set_group(g))
        print("stored")
    elif action == "add":
        ids = [int(x) for x in args.ids.split(",")]
        readings = {r.identifier: r for r in await dev.get_history() if r.identifier in set(ids)}
        missing = sorted(set(ids) - readings.keys())
        if missing:
            raise SD10Error(f"readings not on the device: {missing}")
        _print_group(await dev.add_to_group(args.slot, [readings[i] for i in ids]))
        print(f"added {len(ids)} colour(s)")
    elif action == "remove":
        _print_group(await dev.remove_from_group(args.slot, [int(x) - 1 for x in args.colors.split(",")]))
        print("removed")
    elif action == "from-history":
        ids = {int(x) for x in args.ids.split(",")}
        readings = {r.identifier: r for r in await dev.get_history() if r.identifier in ids}
        missing = sorted(ids - readings.keys())
        if missing:
            raise SD10Error(f"readings not on the device: {missing}")
        colors = [readings[i].lab_under(args.illuminant, args.observer) for i in sorted(ids)]
        g = ColorGroup(args.slot, args.name, colors, illuminant=args.illuminant, observer=args.observer,
                       m_factor=args.m, diff_formula=args.formula, tolerance=args.tolerance)
        _print_group(await dev.set_group(g))
        print("stored")


def _print_row(r) -> None:
    head = f"row {r.row}: {len(r.patches)} patches  {r.illuminant}/{r.observer}deg"
    if r.tolerance is not None:
        head += (f"  mean {r.formula} {r.mean_delta_e:.2f} (max {r.max_delta_e:.2f}, tolerance {r.tolerance:.2f})  "
                 + ("PASS" if r.passed else "FAIL"))
    print(head)
    for x in r.patches:
        line = f"  patch {x.patch:2}  L*={x.lab.L:7.2f}  a*={x.lab.a:7.2f}  b*={x.lab.b:7.2f}"
        if x.delta_e is not None:
            line += f"   dE {x.delta_e:6.2f}" + ("" if x.ok is None else ("  ok" if x.ok else "  NG"))
        print(line)


async def _scan_row(dev: SD10, args) -> None:
    expected = None
    if args.expected:
        with open(args.expected, encoding="utf-8") as f:
            expected = [tuple(c[:3]) for c in json.load(f)]
    print("Place the SD-10 on the first patch of the row and press its button, then slide it to the margin.",
          file=sys.stderr)
    r = await dev.scan_row(expected, patches=args.patches, row=args.row, illuminant=args.illuminant,
                           observer=args.observer, m_factor=args.m, formula=args.formula, tolerance=args.tolerance)
    _print_row(r)


async def _chart(dev: SD10, args) -> None:
    print("For each row: place the SD-10 on the row's first patch, press its button, slide to the margin.",
          file=sys.stderr)
    async for r in dev.scan_palette(args.slot, patches_per_row=args.per_row):
        _print_row(r)


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
             "history": _history, "groups": _groups, "scan-row": _scan_row, "chart": _chart,
             "set-time": _set_time, "set-name": _set_name, "raw": _raw}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="pysd10", description="Epson SD-10 spectrophotometer over Bluetooth LE")
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
    h = sub.add_parser("history", help="download readings stored on the device (its last 100)")
    h.add_argument("--after", type=int, default=0, metavar="ID", help="only readings with a larger record id")
    h.add_argument("--last", type=int, metavar="N", help="only the N most recent")
    h.add_argument("--json", action="store_true")
    h.add_argument("--csv", metavar="FILE", help="write CSV (with spectra) instead of printing")
    h.add_argument("--aco", metavar="FILE", help="write an Adobe Color Swatch file (Photoshop/Illustrator)")
    sr = sub.add_parser("scan-row", help="scan one row of colour patches (slide the device along the row)")
    sr.add_argument("--patches", type=int, help="number of patches in the row (1-18), if no --expected")
    sr.add_argument("--expected", metavar="JSON", help="file with the row's target L*a*b* colours [[L,a,b],...]")
    sr.add_argument("--tolerance", type=float, help="judge each patch against --expected with this dE")
    sr.add_argument("--formula", choices=sorted(p.DIFF_FORMULAS), default="DE2000")
    sr.add_argument("--row", type=int, default=1)
    sr.add_argument("--illuminant", choices=sorted(ILLUMINANTS), default="D50")
    sr.add_argument("--observer", type=int, choices=(2, 10), default=2)
    sr.add_argument("--m", type=int, choices=(0, 1, 2), default=0, help="M-factor")
    ch = sub.add_parser("chart", help="scan a printed chart of a stored palette row by row and compare")
    ch.add_argument("slot", type=int, choices=(1, 2, 3, 4))
    ch.add_argument("--per-row", type=int, default=10, help="patches per row on the chart (1-18, app uses 10)")
    g = sub.add_parser("groups", help="colour groups (palettes) stored on the device: 4 slots, up to 80 colours")
    gs = g.add_subparsers(dest="action", required=True)
    gl = gs.add_parser("list", help="show stored groups")
    gl.add_argument("--json", action="store_true")
    gd = gs.add_parser("delete", help="clear a slot")
    gd.add_argument("slot", type=int, choices=(1, 2, 3, 4))
    ge = gs.add_parser("export", help="save a slot to a JSON file")
    ge.add_argument("slot", type=int, choices=(1, 2, 3, 4))
    ge.add_argument("file", help="JSON file, or .aco for an Adobe Color Swatch")
    gi = gs.add_parser("import", help="store a group from a JSON file (format as 'export')")
    gi.add_argument("file")
    gi.add_argument("--slot", type=int, choices=(1, 2, 3, 4), help="override the slot in the file")
    ga = gs.add_parser("add", help="append readings stored on the device to an existing group")
    ga.add_argument("slot", type=int, choices=(1, 2, 3, 4))
    ga.add_argument("--ids", required=True, help="comma-separated record ids (see 'history')")
    gr = gs.add_parser("remove", help="remove colours from a group")
    gr.add_argument("slot", type=int, choices=(1, 2, 3, 4))
    gr.add_argument("--colors", required=True, help="comma-separated colour numbers as shown by 'groups list'")
    gh = gs.add_parser("from-history", help="build a group from readings stored on the device")
    gh.add_argument("slot", type=int, choices=(1, 2, 3, 4))
    gh.add_argument("name")
    gh.add_argument("--ids", required=True, help="comma-separated record ids (see 'history')")
    gh.add_argument("--illuminant", choices=sorted(ILLUMINANTS), default="D50")
    gh.add_argument("--observer", type=int, choices=(2, 10), default=2)
    gh.add_argument("--m", type=int, choices=(0, 1, 2), default=0, help="M-factor")
    gh.add_argument("--formula", choices=sorted(p.DIFF_FORMULAS), default="DE2000")
    gh.add_argument("--tolerance", type=float, default=1.0)
    sub.add_parser("set-time", help="set the device clock to local time")
    n = sub.add_parser("set-name", help="rename the device (max 16 bytes)")
    n.add_argument("name")
    r = sub.add_parser("raw", help="send a raw framed command")
    r.add_argument("id", help="command id, hex (e.g. 0b)")
    r.add_argument("type", help="type, hex (10/11/12/13)")
    r.add_argument("payload", nargs="?", default="", help="payload hex")
    args = ap.parse_args(argv)

    logging.basicConfig(format="  [%(name)s] %(message)s", stream=sys.stderr)
    logging.getLogger("pysd10").setLevel(logging.DEBUG if args.verbose else logging.WARNING)
    try:
        asyncio.run(_run(args))
    except SD10Error as ex:
        sys.exit(f"error: {ex}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
