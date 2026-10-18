"""Blocking (non-async) wrapper around :class:`sd10.SD10`.

Runs an asyncio loop in a background thread, so it works in plain scripts and inside Jupyter
(which already has a running loop)::

    import sd10
    with sd10.connect_sync() as dev:
        print(dev.measure().lab)
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import inspect
import threading
from typing import Any, Iterator, Optional

from .client import SD10, connect, scan
from .protocol import MeasurementResult


class _LoopThread:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="sd10-loop", daemon=True)
        self.thread.start()

    def run(self, coro, timeout: Optional[float] = None) -> Any:
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return fut.result(timeout)
        except BaseException:
            # Ctrl+C or timeout while the coroutine is still running: cancel it in the loop and let the
            # cancellation finish, so nothing is left pending when the loop is later shut down.
            if not fut.done():
                fut.cancel()
                concurrent.futures.wait([fut], timeout=2)
            raise

    def stop(self) -> None:
        async def shutdown() -> None:
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.loop.shutdown_asyncgens()

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(shutdown(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()


class SyncSD10:
    """Blocking proxy: every async method of :class:`SD10` becomes a normal method here."""

    def __init__(self, address: Optional[str] = None, **connect_kwargs: Any) -> None:
        self._address = address
        self._kwargs = connect_kwargs
        self._lt: Optional[_LoopThread] = None
        self._cm = None
        self._dev: Optional[SD10] = None

    # context manager / explicit open-close
    def open(self) -> "SyncSD10":
        self._lt = _LoopThread()
        self._cm = connect(self._address, **self._kwargs)
        try:
            self._dev = self._lt.run(self._cm.__aenter__())
        except BaseException:
            self._lt.stop()
            raise
        return self

    def close(self) -> None:
        if self._lt is None:
            return
        try:
            self._lt.run(self._cm.__aexit__(None, None, None))
        finally:
            self._lt.stop()
            self._lt = self._dev = self._cm = None

    def __enter__(self) -> "SyncSD10":
        return self.open()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def device(self) -> SD10:
        if self._dev is None:
            raise RuntimeError("not connected; use 'with connect_sync() as dev:' or call open()")
        return self._dev

    def button_measurements(self, *args: Any, **kwargs: Any) -> Iterator[MeasurementResult]:
        """Blocking iterator over measurements taken with the device's button (arguments as
        :meth:`SD10.button_measurements`). Stop it with ``break`` or Ctrl+C (catch
        ``KeyboardInterrupt`` if you don't want the traceback)."""
        agen = self.device.button_measurements(*args, **kwargs)
        lt = self._lt
        try:
            while True:
                yield lt.run(agen.__anext__())
        finally:
            if lt.loop.is_running():
                with contextlib.suppress(BaseException):
                    lt.run(agen.aclose(), timeout=5)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.device, name)
        if inspect.iscoroutinefunction(attr):
            def call(*args: Any, **kwargs: Any) -> Any:
                return self._lt.run(attr(*args, **kwargs))
            call.__name__ = name
            call.__doc__ = attr.__doc__
            return call
        return attr


def connect_sync(address: Optional[str] = None, **connect_kwargs: Any) -> SyncSD10:
    """Blocking version of :func:`sd10.connect`. Use as a context manager or call ``.open()``/``.close()``."""
    return SyncSD10(address, **connect_kwargs)


def scan_sync(timeout: float = 8.0):
    lt = _LoopThread()
    try:
        return lt.run(scan(timeout))
    finally:
        lt.stop()


def measure_once(address: Optional[str] = None, *, illuminant: str = "D50", observer: int = 2, m_factor: int = 0,
                 save: bool = True, **connect_kwargs: Any) -> MeasurementResult:
    """Connect, take one measurement, disconnect. ``sd10.measure_once().lab`` -> Lab(L, a, b)."""
    with connect_sync(address, **connect_kwargs) as dev:
        return dev.measure(illuminant=illuminant, observer=observer, m_factor=m_factor, save=save)
