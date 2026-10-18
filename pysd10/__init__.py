"""Python client for the Epson SD-10 spectrophotometer over Bluetooth LE.

Async::

    import asyncio, pysd10

    async def main():
        async with pysd10.connect() as dev:
            result = await dev.measure(illuminant="D65", observer=10)
            print(result.lab, result.lch)

    asyncio.run(main())

Blocking::

    import pysd10
    print(pysd10.measure_once().lab)

    with pysd10.connect_sync() as dev:
        for result in dev.button_measurements():
            print(result.lab)
"""
from .client import (SD10, CalibrationNeeded, CoverClosed, CoverOpen, DeviceInfo, DeviceNotFound, DeviceRefused,
                     FoundDevice, MeasurementFailed, PatchResult, RowResult, SD10Error, SD10Timeout, connect,
                     find_device, load_app_id, scan)
from .color import (COLOR_SCHEMES, DELTA_E_FORMULAS, ILLUMINANTS, OBSERVERS, WAVELENGTHS, Lab, LCh, color_scheme,
                    delta_e, delta_e_76, delta_e_94, delta_e_2000, lab_to_hex, lab_to_srgb, median_lab,
                    spectrum_to_lab, srgb_to_lab)
from .export import aco_bytes, export_aco, read_aco
from .protocol import ColorGroup, Event, MeasurementResult, Method, Setting, Status
from .sync import SyncSD10, connect_sync, measure_once, scan_sync

__version__ = "0.3.0"  # single source of truth: pyproject.toml reads it (bump here for a release)

__all__ = [
    "SD10", "connect", "scan", "find_device", "load_app_id", "DeviceInfo", "FoundDevice",
    "SyncSD10", "connect_sync", "scan_sync", "measure_once",
    "MeasurementResult", "Status", "Setting", "Event", "Method", "ColorGroup", "PatchResult", "RowResult",
    "Lab", "LCh", "spectrum_to_lab", "delta_e", "delta_e_76", "delta_e_94", "delta_e_2000", "median_lab",
    "color_scheme", "lab_to_srgb", "srgb_to_lab", "lab_to_hex", "COLOR_SCHEMES", "DELTA_E_FORMULAS",
    "ILLUMINANTS", "OBSERVERS", "WAVELENGTHS", "export_aco", "aco_bytes", "read_aco",
    "SD10Error", "SD10Timeout", "DeviceNotFound", "DeviceRefused", "MeasurementFailed", "CoverClosed",
    "CalibrationNeeded", "CoverOpen",
]
