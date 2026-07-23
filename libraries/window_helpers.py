"""Small UI helpers shared by the queue, playlist-art, and visualizer windows."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import tkinter as tk

from libraries.constants import ACCENT_COLOR, TEXT_COLOR

if TYPE_CHECKING:
    from Browser import SongBrowser
    from libraries.song_data import SongInfo


def show_queue_empty_warning(parent: tk.Misc) -> None:
    dlg = tk.Toplevel(parent)
    dlg.title("Queue Empty")
    dlg.configure(bg="#0d0d1a")
    dlg.resizable(False, False)
    dlg.transient(parent)
    dlg.grab_set()
    try:
        _ico = tk.PhotoImage(file=Path(__file__).parent.parent / "Warning.png")
        dlg.iconphoto(False, _ico)
        dlg._ico = _ico
    except Exception:
        pass
    tk.Label(
        dlg,
        text="Add at least one song to the queue first.",
        font=("Segoe UI", 10),
        bg="#0d0d1a", fg=TEXT_COLOR,
        padx=20, pady=16,
    ).pack()
    tk.Button(
        dlg, text="OK",
        font=("Segoe UI", 9),
        bg=ACCENT_COLOR, fg=TEXT_COLOR,
        activebackground="#7a44c0", activeforeground=TEXT_COLOR,
        bd=0, padx=14, pady=6,
        command=dlg.destroy,
    ).pack(pady=(0, 16))
    dlg.update_idletasks()
    x = parent.winfo_rootx() + (parent.winfo_width() - dlg.winfo_width()) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - dlg.winfo_height()) // 2
    dlg.geometry(f"+{x}+{y}")
    dlg.wait_window()


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
        pending = getattr(b, "_search_after_id", None)
        if pending:
            b.after_cancel(pending)
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
