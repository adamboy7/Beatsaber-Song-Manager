"""Small UI helpers shared by the queue, playlist-art, and visualizer windows."""

from __future__ import annotations

from typing import TYPE_CHECKING

import tkinter as tk

from libraries import dialogs

if TYPE_CHECKING:
    from Browser import SongBrowser
    from libraries.song_data import SongInfo


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
