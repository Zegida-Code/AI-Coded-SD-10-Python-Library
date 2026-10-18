"""Python client for the Epson SD-10 spectrophotometer over Bluetooth LE.

Async::

    import asyncio, sd10

    async def main():
        async with sd10.connect() as dev:
            result = await dev.measure(illuminant="D65", observer=10)
            print(result.lab, result.lch)

    asyncio.run(main())

Blocking::

    import sd10
    print(sd10.measure_once().lab)

    with sd10.connect_sync() as dev:
        for result in dev.button_measurements():
            print(result.lab)
"""
from .client import (SD10, DeviceInfo, DeviceNotFound, DeviceRefused, FoundDevice, MeasurementFailed,
                     SD10Error, SD10Timeout, connect, find_device, load_app_id, scan)
from .color import ILLUMINANTS, OBSERVERS, WAVELENGTHS, Lab, LCh, delta_e_76, delta_e_2000, spectrum_to_lab
from .protocol import Event, MeasurementResult, Method, Setting, Status
from .sync import SyncSD10, connect_sync, measure_once, scan_sync

__version__ = "0.1.0"

__all__ = [
    "SD10", "connect", "scan", "find_device", "load_app_id", "DeviceInfo", "FoundDevice",
    "SyncSD10", "connect_sync", "scan_sync", "measure_once",
    "MeasurementResult", "Status", "Setting", "Event", "Method",
    "Lab", "LCh", "spectrum_to_lab", "delta_e_76", "delta_e_2000", "ILLUMINANTS", "OBSERVERS", "WAVELENGTHS",
    "SD10Error", "SD10Timeout", "DeviceNotFound", "DeviceRefused", "MeasurementFailed",
]
