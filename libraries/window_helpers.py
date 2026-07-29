"""Small UI helpers shared by the queue, playlist-art, and visualizer windows."""

from __future__ import annotations

from typing import TYPE_CHECKING

import tkinter as tk

from libraries import dialogs
from libraries import platform_utils

if TYPE_CHECKING:
    from Browser import SongBrowser
    from libraries.song_data import SongInfo


def bind_mousewheel(widget: tk.Misc, handler, add=None) -> None:
    """Bind mouse-wheel scrolling to ``widget`` across platforms.

    Windows and macOS deliver ``<MouseWheel>`` events carrying ``event.delta``;
    X11 (Linux) instead delivers ``<Button-4>`` (up) / ``<Button-5>`` (down)
    press events with no delta at all, so handlers bound only to
    ``<MouseWheel>`` never fire there. This translates those presses into
    delta=+120/−120 and reuses the same handler. The Button-4/5 bindings are
    added only on X11 so the extra mouse buttons Tk maps to those numbers on
    other platforms aren't hijacked.
    """
    widget.bind("<MouseWheel>", handler, add=add)
    if platform_utils.IS_WINDOWS or platform_utils.IS_MAC:
        return

    def _translate(event: tk.Event, delta: int):
        event.delta = delta
        return handler(event)

    widget.bind("<Button-4>", lambda e: _translate(e, 120), add=add)
    widget.bind("<Button-5>", lambda e: _translate(e, -120), add=add)


def show_queue_empty_warning(parent: tk.Misc) -> None:
    dialogs.show_warning(
        "Queue Empty",
        "Add at least one song to the queue first.",
        parent=parent,
    )


def view_song(browser: "SongBrowser", song: "SongInfo") -> None:
    """Select ``song`` in the main library list, searching for it if the
    current filter doesn't include it."""
    b = browser
    folder = str(song.folder)

    def _find(lst):
        # Prefer identity match (same object), fall back to folder path for
        # songs whose SongInfo was replaced after a library reload.
        for i, s in enumerate(lst):
            if s is song or str(s.folder) == folder:
                return i
        return None

    idx = _find(b.filtered)
    if idx is None:
        b.search_var.set("")
        if b._search_after_id:
            b.after_cancel(b._search_after_id)
            b._search_after_id = None
        b._do_search()
        idx = _find(b.filtered)
        if idx is None:
            return
    b.page = idx // b.page_size
    b.selected_indices = {idx}
    b.selected_index = idx
    b._selected_folders = {str(song.folder)}
    b._render_list()
    b._scroll_to_selected()
    b.status_bar.config(text=f"Selected: {song.display_name}")
    b.lift()
    b.focus_force()
