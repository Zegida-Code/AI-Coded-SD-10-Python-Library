"""Helper for test_sync_button_loop_stops_cleanly: the user's script against a fake device.

usage: python _button_scenario.py ctrl_c|break
"""
import asyncio
import signal
import sys
import threading

import sd10
import sd10.client as c
from sd10 import protocol as p
from test_sd10 import FakeBle


class ButtonBle(FakeBle):
    def __init__(self, target):
        super().__init__()

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def start_notify(self, uuid, cb):
        await super().start_notify(uuid, cb)
        loop = asyncio.get_running_loop()

        # two button presses, then silence, so Ctrl+C arrives while blocked waiting for the next one
        for delay in (0.1, 0.3):
            loop.call_later(delay, cb, None, bytearray([p.Event.FINISH_MEASURING]))

    async def write_gatt_char(self, uuid, data, response=True):
        if data[:3] == b"\x00\x43" + bytes([p.Cmd.STANDALONE]):
            self.writes.append(bytes(data))
            self._notify(p.frame(p.Cmd.STANDALONE, 1, b"OK"))
            return
        await super().write_gatt_char(uuid, data, response)


async def fake_find(address, timeout):
    return type("D", (), {"address": "00:11:22:33:44:55", "name": "SD-10"})()


c.BleakClient = ButtonBle
c.find_device = fake_find

def _watchdog():  # never hang the test run if the interrupt does not get through
    import os
    print(f"results={results} (watchdog)", flush=True)
    os._exit(3)


_wd = threading.Timer(15, _watchdog)
_wd.daemon = True
_wd.start()
results = 0
try:
    with sd10.connect_sync(app_id=bytes(16)) as dev:
        if sys.argv[1] == "ctrl_c":
            # Same path as a real Ctrl+C (C-level SIGINT handler), which also wakes a blocked wait on Windows.
            threading.Timer(0.7, signal.raise_signal, (signal.SIGINT,)).start()
        for r in dev.button_measurements():
            results += 1
            if sys.argv[1] == "break" and results == 2:
                break
except KeyboardInterrupt:
    pass
print(f"results={results}")
