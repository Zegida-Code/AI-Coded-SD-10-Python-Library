# pysd10

Python client for the **Epson SD-10** spectrophotometer over Bluetooth LE: take measurements and get
the 31-band spectrum (400–700 nm) and CIE L\*a\*b\* exactly as the Epson Spectrometer app computes it.

## Install

```
pip install pysd10
```

Requires Python 3.9+ and [bleak](https://github.com/hbldh/bleak) (installed automatically).

> **Upgrading from 0.2.x:** the module was renamed to match the package. Change `import sd10` to
> `import pysd10` (or `import pysd10 as sd10` to keep existing code working) and the `sd10` command to
> `pysd10`. Your saved host id is picked up automatically.

## Quick start (blocking — scripts, Jupyter)

```python
import pysd10

result = pysd10.measure_once()            # connect, measure, disconnect
print(result.lab)                       # Lab(L=..., a=..., b=...)

with pysd10.connect_sync() as dev:        # keep the connection open
    r = dev.measure(illuminant="D65", observer=10, m_factor=0)
    print(r.lab, r.lch, r.spectrum)
    print(r.lab_under("A", 2))          # same reading, other illuminant

    for r in dev.button_measurements():  # react to the device's own button
        print(r.lab)
```

## Async

```python
import asyncio, pysd10

async def main():
    async with pysd10.connect() as dev:          # strongest SD-10, or connect("40:84:32:52:FD:0F")
        print(await dev.get_info())
        r = await dev.measure()
        print(r.lab)
        async for r in dev.button_measurements():
            print(r.lab)

asyncio.run(main())
```

## API overview

| | |
|---|---|
| `scan()` / `scan_sync()` | nearby SD-10s (`address`, `name`, `rssi`) |
| `connect(address=None)` / `connect_sync(...)` | connect and open a session; on exit the device is handed back to its own UI |
| `dev.measure(illuminant, observer, m_factor, save)` | one spot measurement → `MeasurementResult` |
| `dev.button_measurements()` | iterator of results taken with the device button |
| `dev.get_history(after_id=0, limit=None, progress=None)` | download readings stored on the device (it keeps its last 100), oldest first; pass the last `identifier` you have as `after_id` to fetch only new ones |
| `dev.history_count(after_id=0)` | how many stored readings are newer than `after_id` |
| `dev.get_groups()` | colour groups (palettes) stored on the device: 4 slots × up to 80 L\*a\*b\* colours → `[ColorGroup]` |
| `dev.set_group(ColorGroup(slot, name, colors, illuminant, observer, m_factor, diff_formula, tolerance))` | store/replace a palette in slot 1–4 |
| `dev.delete_group(slot)` | clear a slot |
| `dev.calibrate()` | white calibration with the cover **closed** (raises `CoverOpen` otherwise) → `Status` |
| `dev.scan_row(expected=None, patches=None, tolerance=None, ...)` | scan one row of ≤18 patches by sliding the device; with `expected` + `tolerance` each patch is judged → `RowResult` |
| `dev.scan_palette_row(slot, row, patches_per_row=10)` | scan one row of a printed chart of a stored palette and compare with it → `RowResult` |
| `dev.scan_palette(slot, patches_per_row=10)` | whole chart, row by row (async iterator of `RowResult`), then `finish_chart()` |
| `dev.measure_palette_patch(slot, patch)` | spot-measure one patch against palette colour `patch` → `PatchResult` |
| `dev.get_info()`, `get_status()`, `get_name()`, `get_serial()`, `get_firmware_version()` | device info |
| `dev.get_setting(Setting.X)` / `set_setting(...)` | buzzer, panel direction, auto power-off, language |
| `dev.set_clock()`, `dev.set_name(...)` | |
| `dev.transact(frame)` | raw command (see `pysd10.protocol`) |
| `MeasurementResult` | `.lab`, `.lch`, `.lab_under(ill, obs)`, `.spectrum`, `.wavelengths`, `.timestamp`, `.to_dict()` |
| `spectrum_to_lab(spectrum, illuminant, observer)` | offline conversion |
| `delta_e(ref, sample, "DE2000" / "DE94GA" / "DE94T" / "DE76")`, `delta_e_2000`, `delta_e_94`, `delta_e_76` | colour difference (matches Epson's library) |
| `median_lab(labs)` | per-component median of several readings (the app's "Median") |
| `color_scheme(lab, "complementary" / "split_complementary" / "analogous" / "triadic" / "tetradic", illuminant, observer)` | the app's "Color Scheme" |
| `lab_to_hex(lab, illuminant, observer)`, `lab_to_srgb`, `srgb_to_lab` | sRGB / HEX as the app shows it |
| `export_aco(path, colors, names)`, `read_aco(path)` | Adobe Color Swatch (.aco) files for Photoshop / Illustrator |

Illuminants: D50, D65, A, F1, F2, F3, F7–F12. Observers: 2, 10.

Errors derive from `pysd10.SD10Error` (`DeviceNotFound`, `DeviceRefused`, `SD10Timeout`, `MeasurementFailed`,
`CoverClosed` — raised instead of measuring when the cover is shut, because the device would just repeat its
previous reading — `CalibrationNeeded`, and `CoverOpen` when calibrating with the cover open).
Enable `logging.getLogger("pysd10").setLevel(logging.DEBUG)` to see all traffic.

## Command line

```
pysd10 scan
pysd10 measure --illuminant D65 --observer 10 [--spectrum] [--json]
pysd10 listen          # L*a*b* for every button press
pysd10 history [--after ID] [--last N] [--json | --csv FILE]   # readings stored on the device
pysd10 history --aco FILE                       # stored readings as an Adobe Color Swatch
pysd10 scan-row --patches 6 | --expected row.json [--tolerance 2]
pysd10 chart SLOT [--per-row 10]                # scan a printed chart of a stored palette
pysd10 groups list | delete SLOT | export SLOT FILE(.json|.aco) | import FILE [--slot N]
pysd10 groups from-history SLOT NAME --ids 951,952,953 [--illuminant D65 --tolerance 2]
pysd10 info | calibrate | set-time | set-name NAME | raw ID TYPE [HEX]
```

(`python -m pysd10 ...` works without installing the script; global options such as `-a ADDRESS`
and `-v` go before the command.)

## Notes

- The SD-10 accepts one host at a time — close the Epson app on your phone first.
- A random 16-byte host id is kept in `~/.pysd10_app_id`, like the app does.
- L\*a\*b\* reproduces Epson's `librmcc` conversion to ~1e-6 ΔE; bands outside 400–700 nm count as
  zero, so a perfect white reads L\* ≈ 99.99, same as the app.