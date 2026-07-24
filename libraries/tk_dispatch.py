"""Thread-safe dispatch of callbacks onto the Tk main thread.

Worker threads must never call a Tk widget's ``.after(...)`` directly —
that relies on CPython/Windows's threaded Tcl build tolerating cross-thread
calls, which Tkinter does not document as safe. Instead, workers call
``Dispatcher.dispatch(callback)``, which only touches ``queue.Queue`` (a
primitive the stdlib does guarantee is thread-safe). A single main-thread
polling loop drains the queue and runs callbacks, so Tk/Tcl is only ever
touched from the thread that owns it.
"""

from __future__ import annotations

import queue
import tkinter as tk


class Dispatcher:
    def __init__(self, interval_ms: int = 20):
        self._queue: queue.Queue = queue.Queue()
        self._widget = None
        self._interval_ms = interval_ms

    def start(self, widget) -> None:
        """Call once from the main thread, passing the root Tk widget."""
        self._widget = widget
        self._pump()

    def dispatch(self, callback) -> None:
        """Thread-safe: schedule callback to run on the main thread ASAP."""
        self._queue.put(callback)

    def _pump(self) -> None:
        while True:
            try:
                callback = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception:
                pass  # one bad callback must not kill the pump
        try:
            self._widget.after(self._interval_ms, self._pump)
        except tk.TclError:
            pass  # root destroyed; let the pump die
