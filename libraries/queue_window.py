"""
Playback queue window for SongBrowser.

A draggable, multi-selectable list of upcoming songs. Shown via
View → Queue from the main menu, or from the player bar context
menu. Hosts its own per-row thumbnails, context menu, and drag-
to-reorder logic.
"""

from __future__ import annotations

import random
from pathlib import Path
from tkinter import messagebox
from typing import TYPE_CHECKING

import tkinter as tk
from PIL import Image, ImageTk
from tkinterdnd2 import DND_FILES

from libraries.audio_utils import get_audio_duration
from libraries.browser_pagination import filter_songs, pick_random_songs
from libraries.constants import (
    ACCENT_COLOR, BG_COLOR, TEXT_COLOR, SUBTEXT_COLOR,
    SELECTED_BG, HOVER_BG, ITEM_BG, SEPARATOR_COLOR, SCROLLBAR_BG,
)
from libraries.song_data import SongInfo

if TYPE_CHECKING:
    from Browser import SongBrowser


def _show_queue_empty_warning(parent: tk.Misc) -> None:
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


_QUEUE_THUMB = (48, 48)
_QUEUE_PLAYING_BG = "#1a1a3a"
_CUT_BG = "#3a1212"


class QueueWindow(tk.Toplevel):
    def __init__(self, browser: "SongBrowser"):
        super().__init__(browser)
        self._browser = browser
        self._selected: set[int] = set()
        self._drag_source: int | None = None
        self._drag_target: int | None = None
        self._dragging: bool = False
        self._drag_start_y: int = 0
        self._thumbnails: dict[str, ImageTk.PhotoImage] = {}
        self._durations: dict[str, float | None] = {}
        self._row_frames: list[tk.Frame] = []
        self._tick_id: str | None = None
        self._last_queue_len: int = -1
        self._last_queue_index: int = -2
        self._last_stopped: bool = False
        self._last_paused: bool = False

        if not hasattr(self._browser, '_queue_clipboard'):
            self._browser._queue_clipboard = []
        # Cut marks are stored as a list of (index, song) tuples so that
        # cutting one occurrence of a song that appears more than once in
        # the queue marks/removes only that specific slot, not every slot
        # holding the same SongInfo reference.
        if not hasattr(self._browser, '_queue_cut_marks'):
            self._browser._queue_cut_marks = None

        self.title("Playback Queue")
        self.configure(bg="#0d0d1a")
        self.geometry("420x500")
        _icon = tk.PhotoImage(file=Path(__file__).parent.parent / "Playlist.png")
        self.iconphoto(False, _icon)
        self._icon = _icon
        self.minsize(340, 200)

        header = tk.Frame(self, bg="#0d0d1a")
        header.pack(fill="x", padx=12, pady=(10, 4))
        self._header_label = tk.Label(
            header, text="Queue",
            font=("Segoe UI", 13, "bold"),
            bg="#0d0d1a", fg=TEXT_COLOR,
            cursor="hand2",
        )
        self._header_label.pack(side="left")
        self._header_label.bind("<Button-3>", self._on_header_right_click)
        self._header_label.bind("<Button-1>", self._on_header_left_click)
        self._play_btn = tk.Button(
            header, text="▶",
            font=("Segoe UI", 14),
            bg="#0d0d1a", fg=TEXT_COLOR,
            activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR,
            relief="flat", bd=0, padx=3, pady=0,
            cursor="hand2",
            command=self._on_play_btn_click,
        )
        self._play_btn.pack(side="left", padx=(6, 0))
        self._shuffle_btn = tk.Button(
            header, text="🔀",
            font=("Segoe UI", 15),
            bg="#0d0d1a", fg=TEXT_COLOR,
            activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR,
            relief="flat", bd=0, padx=3, pady=0,
            cursor="hand2",
            command=self._on_shuffle_btn_click,
        )
        self._shuffle_btn.pack(side="left", padx=(4, 0), pady=(0, 1))
        self._repeat_btn = tk.Button(
            header, text="🔁",
            font=("Segoe UI", 15),
            bg="#0d0d1a", fg=TEXT_COLOR,
            activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR,
            relief="flat", bd=0, padx=2, pady=0,
            cursor="hand2",
            command=self._on_repeat_btn_click,
        )
        self._repeat_btn.pack(side="left", padx=(2, 0), pady=(0, 1))
        self._next_btn = tk.Button(
            header, text="⏭",
            font=("Segoe UI", 14),
            bg="#0d0d1a", fg=TEXT_COLOR,
            activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR,
            relief="flat", bd=0, padx=4,
            command=browser._queue_next,
        )
        self._next_btn.pack(side="right")
        self._back_btn = tk.Button(
            header, text="⏮",
            font=("Segoe UI", 14),
            bg="#0d0d1a", fg=TEXT_COLOR,
            activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR,
            relief="flat", bd=0, padx=4,
            command=browser._queue_prev,
        )
        self._back_btn.pack(side="right", padx=(0, 2))

        container = tk.Frame(self, bg="#0d0d1a")
        container.pack(fill="both", expand=True)

        self._canvas = tk.Canvas(container, bg="#0d0d1a", highlightthickness=0)
        scrollbar = tk.Scrollbar(container, orient="vertical",
                                 command=self._canvas.yview,
                                 bg=SCROLLBAR_BG)
        self._canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        self._list_frame = tk.Frame(self._canvas, bg="#0d0d1a")
        self._canvas_win = self._canvas.create_window((0, 0), window=self._list_frame, anchor="nw")
        self._drag_indicator = tk.Frame(self, bg=ACCENT_COLOR, height=2)

        self._list_frame.bind("<Configure>", lambda e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfig(
            self._canvas_win, width=e.width))
        self._canvas.bind("<MouseWheel>", self._on_mousewheel)
        self._list_frame.bind("<MouseWheel>", self._on_mousewheel)

        self.bind("<Delete>", self._delete_selected)
        self.bind("<BackSpace>", self._delete_selected)
        self.bind("<Escape>", self._deselect_all)
        self.bind("<Control-a>", self._select_all)
        self.bind("<Control-s>", self._on_save_shortcut)
        self.bind("<Control-o>", lambda _: self._open_playlist())
        self.bind("<Control-c>", self._copy_selected)
        self.bind("<Control-x>", self._cut_selected)
        self.bind("<Control-v>", self._paste_clipboard)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._setup_dnd()
        self.refresh()
        self._tick_id = self.after(300, self._tick)

    # ── Playlist drag-and-drop ────────────────────────────────────────────────

    def _setup_dnd(self):
        for widget in (self, self._canvas, self._list_frame):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind('<<Drop>>', self._on_dnd_drop)
            widget.dnd_bind('<<DropEnter>>', self._on_dnd_enter)
            widget.dnd_bind('<<DropLeave>>', self._on_dnd_leave)

    def _on_dnd_enter(self, _event):
        self._canvas.configure(bg="#1a1a2e")
        self._list_frame.configure(bg="#1a1a2e")

    def _on_dnd_leave(self, _event):
        self._canvas.configure(bg="#0d0d1a")
        self._list_frame.configure(bg="#0d0d1a")

    def _on_dnd_drop(self, event):
        self._canvas.configure(bg="#0d0d1a")
        self._list_frame.configure(bg="#0d0d1a")
        path = self.tk.splitlist(event.data)[0]
        if Path(path).suffix.lower() not in {".bplist", ".json"}:
            return
        self._browser._load_playlist_to_queue(path, anchor=self)

    def _on_save_shortcut(self, _event=None):
        if self._browser._queue:
            self._browser._share_playlist(list(self._browser._queue), parent=self)
        else:
            _show_queue_empty_warning(self)

    def _open_playlist(self, _event=None):
        import tkinter.filedialog as fd
        path = fd.askopenfilename(
            title="Open Playlist",
            filetypes=[("Beat Saber Playlist", "*.bplist"), ("All files", "*.*")],
            parent=self,
        )
        if not path:
            return
        self._browser._load_playlist_to_queue(path, anchor=self)

    def _on_close(self):
        if self._tick_id:
            self.after_cancel(self._tick_id)
        self._browser._queue_cut_marks = None  # un-mark cuts; clipboard survives
        self._browser._queue_window = None
        self.destroy()

    def _tick(self):
        new_len = len(self._browser._queue)
        new_idx = self._browser._queue_index
        new_stopped = self._browser._media_player._stopped
        new_paused = self._browser._media_player._audio_paused
        if new_len != self._last_queue_len:
            self.refresh()
        elif new_idx != self._last_queue_index or new_stopped != self._last_stopped or new_paused != self._last_paused:
            self._update_row_colors()
            self._last_queue_index = new_idx
            self._last_stopped = new_stopped
            self._last_paused = new_paused
        self._refresh_nav_btns()
        self._refresh_play_btn()
        self._refresh_shuffle_btn()
        self._refresh_repeat_btn()
        self._tick_id = self.after(300, self._tick)

    def _refresh_nav_btns(self):
        b = self._browser
        mp = b._media_player
        if mp._looping or not b._queue:
            can_next = can_prev = False
        else:
            can_next = (
                b._queue_index + 1 < len(b._queue)
                or (b._shuffle_queue and len(b._queue) >= 2)
                or b._loop_queue
            )
            can_prev = b._queue_index > 0 or b._loop_queue
        self._next_btn.config(
            state="normal" if can_next else "disabled",
            fg=TEXT_COLOR if can_next else SUBTEXT_COLOR,
        )
        self._back_btn.config(
            state="normal" if can_prev else "disabled",
            fg=TEXT_COLOR if can_prev else SUBTEXT_COLOR,
        )

    def _on_play_btn_click(self, _event=None):
        b = self._browser
        mp = b._media_player
        if not b._queue:
            return
        if mp._stopped:
            b._play_audio(b._queue[b._queue_index])
        else:
            mp.toggle_pause()

    def _refresh_play_btn(self):
        b = self._browser
        mp = b._media_player
        has_queue = bool(b._queue)
        is_playing = has_queue and not mp._stopped and not mp._audio_paused
        self._play_btn.config(
            text="⏸" if is_playing else "▶",
            fg=TEXT_COLOR if has_queue else SUBTEXT_COLOR,
            state="normal" if has_queue else "disabled",
            cursor="hand2" if has_queue else "",
        )

    def _on_shuffle_btn_click(self):
        self._browser._toggle_shuffle_queue()
        self._refresh_shuffle_btn()
        self._refresh_nav_btns()

    def _on_repeat_btn_click(self):
        self._browser._toggle_loop()
        self._refresh_repeat_btn()
        self._refresh_nav_btns()

    def _refresh_shuffle_btn(self):
        b = self._browser
        can_shuffle = len(b._queue) >= 2
        active = b._shuffle_queue and can_shuffle
        self._shuffle_btn.config(
            fg=ACCENT_COLOR if active else (TEXT_COLOR if can_shuffle else SUBTEXT_COLOR),
            state="normal" if can_shuffle else "disabled",
            cursor="hand2" if can_shuffle else "",
        )

    def _refresh_repeat_btn(self):
        b = self._browser
        has_queue = bool(b._queue)
        active = b._media_player._looping
        self._repeat_btn.config(
            fg=ACCENT_COLOR if active else (TEXT_COLOR if has_queue else SUBTEXT_COLOR),
            state="normal" if has_queue else "disabled",
            cursor="hand2" if has_queue else "",
        )

    # ── Build / Refresh ──────────────────────────────────────────────────────

    def refresh(self):
        for w in self._list_frame.winfo_children():
            w.destroy()
        self._row_frames.clear()
        self._selected.clear()

        queue = self._browser._queue
        self._last_queue_len = len(queue)
        self._last_queue_index = self._browser._queue_index

        if not queue:
            tk.Label(
                self._list_frame, text="Queue is empty",
                font=("Segoe UI", 10),
                bg="#0d0d1a", fg=SUBTEXT_COLOR,
                anchor="center",
            ).pack(pady=30)
            self._refresh_nav_btns()
            self._refresh_play_btn()
            self._refresh_shuffle_btn()
            self._refresh_repeat_btn()
            return

        for i, song in enumerate(queue):
            self._build_row(i, song)
        self._refresh_nav_btns()
        self._refresh_play_btn()
        self._refresh_shuffle_btn()
        self._refresh_repeat_btn()

    def _row_bg(self, idx: int) -> str:
        if idx in self._selected:
            return SELECTED_BG
        if idx == self._browser._queue_index:
            return _QUEUE_PLAYING_BG
        return ITEM_BG

    def _build_row(self, idx: int, song: "SongInfo"):
        bg = self._row_bg(idx)
        is_playing = (idx == self._browser._queue_index)

        row = tk.Frame(self._list_frame, bg=bg, cursor="fleur")
        row.pack(fill="x")
        self._row_frames.append(row)

        stopped = self._browser._media_player._stopped
        paused = self._browser._media_player._audio_paused
        if is_playing and stopped:
            row_icon, row_fg = "■", SUBTEXT_COLOR
        elif is_playing and paused:
            row_icon, row_fg = "▌▌", ACCENT_COLOR
        elif is_playing:
            row_icon, row_fg = "▶", ACCENT_COLOR
        else:
            row_icon, row_fg = str(idx + 1), SUBTEXT_COLOR
        num_lbl = tk.Label(
            row,
            text=row_icon,
            font=("Segoe UI", 9, "bold"),
            bg=bg, fg=row_fg,
            width=3, anchor="center",
        )
        num_lbl.pack(side="left", padx=(8, 4), pady=6)

        thumb_img = self._load_thumb(song)
        thumb_lbl = tk.Label(row, image=thumb_img, bg=bg)
        thumb_lbl.image = thumb_img
        thumb_lbl.pack(side="left", padx=(0, 8), pady=6)

        text_frame = tk.Frame(row, bg=bg)
        text_frame.pack(side="left", fill="both", expand=True, padx=4, pady=6)

        tk.Label(
            text_frame, text=song.display_name,
            font=("Segoe UI", 10, "bold"),
            bg=bg, fg=TEXT_COLOR, anchor="w",
        ).pack(fill="x")
        subtitle = self._subtitle(song)
        if subtitle:
            tk.Label(
                text_frame, text=subtitle,
                font=("Segoe UI", 8),
                bg=bg, fg=SUBTEXT_COLOR, anchor="w",
            ).pack(fill="x")

        sep = tk.Frame(self._list_frame, bg=SEPARATOR_COLOR, height=1)
        sep.pack(fill="x")

        widgets = [row, num_lbl, thumb_lbl, text_frame]
        for child in text_frame.winfo_children():
            widgets.append(child)
        for w in widgets:
            w.bind("<ButtonPress-1>",   lambda e, i=idx: self._on_press(e, i))
            w.bind("<B1-Motion>",       lambda e, i=idx: self._on_b1_motion(e, i))
            w.bind("<ButtonRelease-1>", lambda e, i=idx: self._on_release(e, i))
            w.bind("<Button-3>",        lambda e, i=idx, s=song: self._on_right_click(e, i, s))
            w.bind("<Enter>",           lambda e, r=row, i=idx: self._on_enter(r, i))
            w.bind("<Leave>",           lambda e, r=row, i=idx: self._on_leave(r, i))
            w.bind("<MouseWheel>",      self._on_mousewheel)

    # ── Context Menu ─────────────────────────────────────────────────────────

    def _on_header_left_click(self, _event: tk.Event):
        total = len(self._row_frames)
        if total and self._selected == set(range(total)):
            self._deselect_all()
        else:
            self._select_all()

    def _on_header_right_click(self, event: tk.Event):
        queue = self._browser._queue
        menu = tk.Menu(
            self, tearoff=0,
            bg="#1e1e1e", fg=TEXT_COLOR,
            activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR, bd=0,
        )
        menu.add_command(
            label="Save Queue",
            state="normal" if queue else "disabled",
            command=lambda: self._browser._share_playlist(list(queue), parent=self),
        )
        menu.add_separator()
        menu.add_command(
            label="Shuffle Order",
            state="normal" if len(queue) > 1 else "disabled",
            command=self._shuffle_queue_order,
        )
        def _open_add_random():
            r = self._show_add_random_dialog()
            if r:
                self._add_random_songs(*r)
        menu.add_command(label="Add Random Song…", command=_open_add_random)
        b = self._browser
        mp = b._media_player
        is_playing = queue and not mp._stopped
        menu.add_command(
            label="Stop",
            state="normal" if is_playing else "disabled",
            command=b._stop_playback,
        )
        menu.add_separator()
        menu.add_command(
            label="Clear Queue",
            foreground="#ff4444",
            activeforeground="#ff4444",
            state="normal" if queue else "disabled",
            command=self._confirm_clear_queue,
        )
        menu.tk_popup(event.x_root, event.y_root)

    def _shuffle_queue_order(self):
        b = self._browser
        queue = b._queue
        if len(queue) < 2:
            return
        curr = b._queue_index
        # Permute by indices so duplicate SongInfo references don't confuse the
        # "where is the playing song now?" lookup.
        perm = list(range(len(queue)))
        random.shuffle(perm)
        queue[:] = [queue[i] for i in perm]
        if 0 <= curr < len(queue):
            b._queue_index = perm.index(curr)
        self.refresh()

    def _show_add_random_dialog(
        self, locked_count: int | None = None
    ) -> "tuple[str, int] | None":
        result: dict = {"query": None, "n": None}

        dlg = tk.Toplevel(self)
        dlg.title("Add Random Song")
        dlg.configure(bg=BG_COLOR)
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()
        try:
            _rand_icon = tk.PhotoImage(file=Path(__file__).parent.parent / "Random.png")
            dlg.iconphoto(False, _rand_icon)
            dlg._rand_icon = _rand_icon
        except Exception:
            pass

        pad = {"padx": 12, "pady": 6}

        filter_frame = tk.Frame(dlg, bg=BG_COLOR)
        filter_frame.pack(fill="x", **pad)
        tk.Label(filter_frame, text="Filter:", bg=BG_COLOR, fg=TEXT_COLOR,
                 font=("Segoe UI", 9), width=7, anchor="w").pack(side="left")
        filter_var = tk.StringVar()
        filter_entry = tk.Entry(
            filter_frame, textvariable=filter_var,
            bg="#1e1e1e", fg=TEXT_COLOR, insertbackground=TEXT_COLOR,
            relief="flat", font=("Segoe UI", 9),
        )
        filter_entry.pack(side="left", fill="x", expand=True)

        tk.Label(
            dlg,
            text="  Supports plain text, {artist}:, {mapper}:, {title}:, {unplayed}:y, {favorite}:y",
            bg=BG_COLOR, fg=SUBTEXT_COLOR, font=("Segoe UI", 7), anchor="w",
        ).pack(fill="x", padx=12)

        bottom_frame = tk.Frame(dlg, bg=BG_COLOR)
        bottom_frame.pack(fill="x", padx=12, pady=(4, 10))

        tk.Label(bottom_frame, text="Count:", bg=BG_COLOR, fg=TEXT_COLOR,
                 font=("Segoe UI", 9)).pack(side="left")
        count_var = tk.StringVar(value=str(locked_count) if locked_count is not None else "1")
        tk.Spinbox(
            bottom_frame, from_=1, to=999, textvariable=count_var,
            bg="#1e1e1e", fg=TEXT_COLOR, buttonbackground="#1e1e1e",
            insertbackground=TEXT_COLOR, relief="flat", font=("Segoe UI", 9),
            width=6,
            state="disabled" if locked_count is not None else "normal",
        ).pack(side="left", padx=(4, 0))

        def _ok(_event=None):
            try:
                n = max(1, int(count_var.get()))
            except ValueError:
                n = 1
            result["query"] = filter_var.get().strip()
            result["n"] = n
            dlg.destroy()

        def _cancel(_event=None):
            dlg.destroy()

        tk.Button(
            bottom_frame, text="Cancel", command=_cancel,
            bg="#1e1e1e", fg=TEXT_COLOR, activebackground="#333",
            activeforeground=TEXT_COLOR, relief="flat", font=("Segoe UI", 9),
        ).pack(side="right", padx=(4, 0))
        tk.Button(
            bottom_frame, text="Add", command=_ok,
            bg=ACCENT_COLOR, fg=TEXT_COLOR, activebackground="#7a00cc",
            activeforeground=TEXT_COLOR, relief="flat", font=("Segoe UI", 9, "bold"),
        ).pack(side="right")

        dlg.bind("<Return>", _ok)
        dlg.bind("<Escape>", _cancel)
        filter_entry.focus_set()
        self.wait_window(dlg)

        if result["n"] is not None:
            return (result["query"] or "", result["n"])
        return None

    def _add_random_songs(self, query: str, n: int):
        b = self._browser
        all_songs = b.songs
        queue_folders = {str(s.folder) for s in b._queue}

        filtered_not_in_queue = None
        if query:
            filtered = filter_songs(all_songs, query, b.player_stats, b.favorite_ids)
            filtered_not_in_queue = [s for s in filtered if str(s.folder) not in queue_folders]

        all_not_in_queue = [s for s in all_songs if str(s.folder) not in queue_folders]
        unfiltered_pool = all_not_in_queue if all_not_in_queue else all_songs

        picks = pick_random_songs(filtered_not_in_queue, unfiltered_pool, n)
        if picks:
            b._add_to_queue(picks)

    def _random_replace(self, idx: int):
        if idx in self._selected and len(self._selected) > 1:
            sorted_indices = sorted(self._selected)
            locked = len(sorted_indices)
        else:
            sorted_indices = [idx]
            locked = None

        r = self._show_add_random_dialog(locked_count=locked)
        if not r:
            return
        query, n = r

        b = self._browser
        all_songs = b.songs

        # Exclude queue songs except the ones being replaced (they're leaving)
        replacing_folders = {str(b._queue[i].folder) for i in sorted_indices}
        queue_folders = {str(s.folder) for s in b._queue} - replacing_folders

        filtered_pool = None
        if query:
            filtered = filter_songs(all_songs, query, b.player_stats, b.favorite_ids)
            filtered_pool = [s for s in filtered if str(s.folder) not in queue_folders]

        unfiltered_pool = [s for s in all_songs if str(s.folder) not in queue_folders]
        if not unfiltered_pool:
            unfiltered_pool = all_songs

        picks = pick_random_songs(filtered_pool, unfiltered_pool, n)
        if not picks:
            return

        queue = b._queue
        curr = b._queue_index
        playing_replaced = curr in set(sorted_indices)

        if len(sorted_indices) > 1:
            new_playing_song = None
            for i, qi in enumerate(sorted_indices):
                queue[qi] = picks[i]
                if qi == curr:
                    new_playing_song = picks[i]
            if playing_replaced and new_playing_song is not None:
                b._queue_index = curr
                b._play_audio(new_playing_song)
        else:
            qi = sorted_indices[0]
            queue[qi : qi + 1] = picks
            if playing_replaced:
                b._queue_index = qi
                b._play_audio(picks[0])
            elif curr > qi:
                b._queue_index = curr + (len(picks) - 1)

        self._selected.clear()
        self.refresh()

    def _confirm_clear_queue(self):
        if not messagebox.askyesno(
            "Clear Queue",
            "Stop playback and clear the entire queue?",
            icon="warning",
            default="no",
        ):
            return
        self._browser._stop_player()
        self.refresh()

    def _on_right_click(self, event: tk.Event, idx: int, song: "SongInfo"):
        queue = self._browser._queue
        b = self._browser
        mp = b._media_player
        is_active = (idx == b._queue_index and not mp._stopped)
        menu = tk.Menu(
            self, tearoff=0,
            bg="#1e1e1e", fg=TEXT_COLOR,
            activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR, bd=0,
        )
        if is_active:
            menu.add_command(label="Stop", command=b._stop_playback)
        else:
            menu.add_command(label="Play", command=lambda: self._play_from_queue(idx, song))
        menu.add_command(label="View Song", command=lambda: self._view_song(song))
        menu.add_command(label="Random Replace…", command=lambda: self._random_replace(idx))
        menu.add_separator()
        menu.add_command(
            label="Move to Top",
            state="normal" if idx > 0 else "disabled",
            command=lambda: self._move_to_top(idx),
        )
        menu.add_command(
            label="Move to Bottom",
            state="normal" if idx < len(queue) - 1 else "disabled",
            command=lambda: self._move_to_bottom(idx),
        )
        menu.add_separator()
        shuffle_var = tk.BooleanVar(value=b._shuffle_queue)
        can_shuffle = len(queue) >= 2
        menu.add_checkbutton(
            label="Shuffle", variable=shuffle_var,
            command=b._toggle_shuffle_queue,
            selectcolor=ACCENT_COLOR,
            state="normal" if can_shuffle else "disabled",
        )
        loop_var = tk.BooleanVar(value=mp._looping)
        menu.add_checkbutton(
            label="Loop", variable=loop_var,
            command=lambda: self._loop_song(idx, song),
            selectcolor=ACCENT_COLOR,
        )
        menu.tk_popup(event.x_root, event.y_root)

    def _view_song(self, song: "SongInfo"):
        b = self._browser
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

    def _play_from_queue(self, idx: int, song: "SongInfo"):
        self._browser._queue_index = idx
        self._browser._play_audio(song)

    def _move_to_top(self, idx: int):
        if idx > 0:
            self._perform_move(idx, 0)

    def _move_to_bottom(self, idx: int):
        queue = self._browser._queue
        if idx < len(queue) - 1:
            self._perform_move(idx, len(queue))

    def _loop_song(self, idx: int, song: "SongInfo"):
        b = self._browser
        mp = b._media_player
        if b._queue_index != idx:
            b._queue_index = idx
            b._play_audio(song)
        mp.toggle_loop()
        b._show_player_bar(song)

    def _subtitle(self, song: "SongInfo") -> str:
        key = str(song.folder)
        # Don't call ffprobe synchronously on the UI thread — for a long queue
        # the first render would block per row at the 5s ffprobe timeout. Kick
        # the lookup off in a background thread and refresh once it returns.
        if key not in self._durations and song.audio_path:
            self._durations[key] = None  # mark as in-flight so we don't relaunch
            self._fetch_duration_async(key, song.audio_path)
        dur = self._durations.get(key)
        parts = []
        if song.author:
            parts.append(song.author)
        if dur is not None:
            m, s = divmod(int(dur), 60)
            parts.append(f"{m}:{s:02d}")
        return "  •  ".join(parts)

    def _fetch_duration_async(self, key: str, audio_path) -> None:
        import threading

        def worker():
            try:
                dur = get_audio_duration(audio_path)
            except Exception:
                dur = None
            try:
                self.after(0, lambda: self._apply_duration(key, dur))
            except tk.TclError:
                pass  # window was destroyed

        threading.Thread(target=worker, daemon=True).start()

    def _apply_duration(self, key: str, dur) -> None:
        if dur is None:
            return
        self._durations[key] = dur
        # A duration just arrived — re-render so the row text updates.
        try:
            self.refresh()
        except tk.TclError:
            pass

    def _get_placeholder_thumb(self) -> ImageTk.PhotoImage:
        """Return a single shared placeholder thumbnail (cached on first use)."""
        cached = getattr(self, "_placeholder_thumb", None)
        if cached is not None:
            return cached
        try:
            placeholder = Image.new("RGB", _QUEUE_THUMB, color="#2a0033")
            self._placeholder_thumb = ImageTk.PhotoImage(placeholder)
        except Exception:
            self._placeholder_thumb = tk.PhotoImage(
                width=_QUEUE_THUMB[0], height=_QUEUE_THUMB[1]
            )
        return self._placeholder_thumb

    def _load_thumb(self, song: "SongInfo") -> ImageTk.PhotoImage:
        key = str(song.folder)
        if key in self._thumbnails:
            return self._thumbnails[key]
        try:
            if song.cover_path:
                img = Image.open(song.cover_path).convert("RGB")
                img = img.resize(_QUEUE_THUMB, Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                self._thumbnails[key] = photo
                return photo
        except Exception:
            pass
        # Reuse a single placeholder across all cover-less rows instead of
        # creating N identical PhotoImages.
        return self._get_placeholder_thumb()

    def invalidate_caches(self) -> None:
        """Clear thumbnail and duration caches — call after a library reload."""
        self._thumbnails.clear()
        self._durations.clear()

    # ── Drag-to-Reorder ──────────────────────────────────────────────────────

    def _on_press(self, event: tk.Event, idx: int):
        self._drag_source = idx
        self._drag_start_y = event.y_root
        self._dragging = False
        self._drag_target = None

    def _on_b1_motion(self, event: tk.Event, source_idx: int):
        if abs(event.y_root - self._drag_start_y) > 5 and not self._dragging:
            self._dragging = True
            if len(self._selected) > 1:
                self._selected = {self._drag_source} if self._drag_source is not None else set()
                self._update_row_colors()
            if 0 <= self._drag_source < len(self._row_frames):
                self._recolor_row(self._row_frames[self._drag_source],
                                  self._drag_source, "#222244")
        if not self._dragging or self._drag_source is None:
            return
        self._drag_target = self._find_gap_at_y(event.y_root)
        self._show_drop_indicator(self._drag_target)

    def _on_release(self, event: tk.Event, source_idx: int):
        src = self._drag_source
        dst = self._drag_target
        was_dragging = self._dragging
        self._drag_source = None
        self._drag_target = None
        self._dragging = False
        self._drag_indicator.place_forget()
        if was_dragging and src is not None and dst is not None \
                and dst != src and dst != src + 1:
            self._perform_move(src, dst)
        elif was_dragging:
            self.refresh()
        else:
            self._on_click(event, source_idx)

    def _find_gap_at_y(self, y_root: int) -> int:
        for i, row in enumerate(self._row_frames):
            try:
                if y_root < row.winfo_rooty() + row.winfo_height() / 2:
                    return i
            except tk.TclError:
                pass
        return len(self._row_frames)

    def _show_drop_indicator(self, gap: int):
        if not self._row_frames:
            self._drag_indicator.place_forget()
            return
        if gap < len(self._row_frames):
            y_root = self._row_frames[gap].winfo_rooty()
        else:
            r = self._row_frames[-1]
            y_root = r.winfo_rooty() + r.winfo_height()
        y_win = y_root - self.winfo_rooty()
        self._drag_indicator.place(
            x=self._canvas.winfo_x(), y=y_win,
            width=self._canvas.winfo_width(), height=2,
        )
        self._drag_indicator.lift()

    def _perform_move(self, src: int, dst: int):
        queue = self._browser._queue
        curr = self._browser._queue_index

        song = queue.pop(src)
        if dst > src:
            dst -= 1
        queue.insert(dst, song)

        # Track the playing index by arithmetic on src/dst rather than by
        # identity — duplicate SongInfo refs would land on the wrong copy.
        if 0 <= curr < len(queue):
            if curr == src:
                self._browser._queue_index = dst
            else:
                mid = curr - 1 if curr > src else curr
                self._browser._queue_index = mid + 1 if mid >= dst else mid
        self.refresh()

    # ── Selection & Delete ───────────────────────────────────────────────────

    def _on_click(self, event: tk.Event, idx: int):
        ctrl = bool(event.state & 0x4)
        shift = bool(event.state & 0x1)
        if ctrl:
            if idx in self._selected:
                self._selected.discard(idx)
            else:
                self._selected.add(idx)
        elif shift and len(self._selected) == 1:
            anchor = next(iter(self._selected))
            lo, hi = min(anchor, idx), max(anchor, idx)
            self._selected = set(range(lo, hi + 1))
        elif shift:
            if idx in self._selected:
                self._selected.discard(idx)
            else:
                self._selected.add(idx)
        elif len(self._selected) == 1 and idx in self._selected:
            self._selected.discard(idx)
        else:
            self._selected = {idx}
        self._update_row_colors()

    def _select_all(self, _event=None):
        self._selected = set(range(len(self._row_frames)))
        self._update_row_colors()

    def _deselect_all(self, _event=None):
        self._selected.clear()
        self._update_row_colors()

    def _delete_selected(self, event=None):
        if not self._selected:
            return
        to_delete = sorted(self._selected, reverse=True)
        curr = self._browser._queue_index
        playing_deleted = curr in self._selected
        for i in to_delete:
            del self._browser._queue[i]
            if i < curr:
                curr -= 1
        if playing_deleted:
            self._browser._stop_audio_keep_queue()
        else:
            self._browser._queue_index = curr
        self.refresh()

    # ── Clipboard Operations ──────────────────────────────────────────────────

    def _copy_selected(self, event=None):
        if not self._selected:
            return
        queue = self._browser._queue
        self._browser._queue_clipboard = [queue[i] for i in sorted(self._selected)]
        self._browser._queue_cut_marks = None
        self._selected.clear()
        self._update_row_colors()

    def _cut_selected(self, event=None):
        if not self._selected:
            return
        queue = self._browser._queue
        selected_sorted = sorted(self._selected)
        self._browser._queue_clipboard = [queue[i] for i in selected_sorted]
        # Record the (index, song) of each cut slot. The song reference is
        # used at paste time to validate that the queue hasn't shifted under
        # us; duplicates of the same SongInfo are distinguished by index.
        self._browser._queue_cut_marks = [(i, queue[i]) for i in selected_sorted]
        self._selected.clear()
        self._update_row_colors()

    def _paste_clipboard(self, event=None):
        clipboard = getattr(self._browser, '_queue_clipboard', [])
        if not clipboard:
            return
        queue = self._browser._queue
        cut_marks = getattr(self._browser, '_queue_cut_marks', None)

        # Determine insert position
        if len(self._selected) == 1:
            insert_idx = sorted(self._selected)[0] + 1
        else:
            insert_idx = len(queue)

        playing_was_cut = False
        next_to_play_idx = -1  # index in the post-removal, pre-paste queue
        curr = self._browser._queue_index

        if cut_marks:
            # Validate each cut mark: only remove a slot if its recorded
            # SongInfo is still at that index. This makes the cut mark
            # specific to a single queue slot, so a duplicated song with
            # only one occurrence cut won't pull both occurrences out.
            cut_indices = sorted({
                i for i, s in cut_marks
                if 0 <= i < len(queue) and queue[i] is s
            })
            cut_idx_set = set(cut_indices)

            playing_was_cut = 0 <= curr < len(queue) and curr in cut_idx_set

            if playing_was_cut:
                # First non-cut index after curr → translate to post-removal index.
                for i in range(curr + 1, len(queue)):
                    if i not in cut_idx_set:
                        next_to_play_idx = i - sum(1 for ci in cut_indices if ci <= i)
                        break

            # Shift insert_idx for each cut song that precedes it
            insert_idx -= sum(1 for ci in cut_indices if ci < insert_idx)

            # Remove cut songs in reverse order
            for i in sorted(cut_indices, reverse=True):
                queue.pop(i)

            if not playing_was_cut and 0 <= curr:
                self._browser._queue_index = (
                    curr - sum(1 for ci in cut_indices if ci < curr)
                )

            self._browser._queue_cut_marks = None

        # Clamp
        insert_idx = max(0, min(insert_idx, len(queue)))

        # Shift playing index past the incoming block (skip if we'll set it explicitly below)
        if not playing_was_cut:
            c = self._browser._queue_index
            if c >= 0 and c >= insert_idx:
                self._browser._queue_index = c + len(clipboard)

        # Insert clipboard songs as a block
        for j, song in enumerate(clipboard):
            queue.insert(insert_idx + j, song)

        # Resolve playback for a cut-and-removed playing song
        if playing_was_cut:
            if next_to_play_idx >= 0:
                # The recorded index was in the post-removal queue; account for
                # the just-inserted paste block.
                if next_to_play_idx >= insert_idx:
                    next_to_play_idx += len(clipboard)
                self._browser._queue_index = next_to_play_idx
                if 0 <= next_to_play_idx < len(queue):
                    self._browser._play_audio(queue[next_to_play_idx])
                else:
                    self._browser._stop_audio_keep_queue()
            else:
                self._browser._stop_audio_keep_queue()

        self._selected.clear()
        self.refresh()

    # ── Row Coloring ─────────────────────────────────────────────────────────

    def _update_row_colors(self):
        playing_idx = self._browser._queue_index
        queue = self._browser._queue
        cut_marks = getattr(self._browser, '_queue_cut_marks', None)
        # Honour only marks whose recorded (index, song) still matches the
        # current queue — that way a single occurrence of a duplicated song
        # stays highlighted while its twin doesn't.
        cut_indices = (
            {i for i, s in cut_marks if 0 <= i < len(queue) and queue[i] is s}
            if cut_marks else set()
        )
        for i, row in enumerate(self._row_frames):
            if self._dragging and i == self._drag_source:
                continue
            elif i in self._selected:
                bg = SELECTED_BG
            elif i in cut_indices:
                bg = _CUT_BG
            elif i == playing_idx:
                bg = _QUEUE_PLAYING_BG
            else:
                bg = ITEM_BG
            self._recolor_row(row, i, bg)
        self._last_queue_index = playing_idx

    def _recolor_row(self, row: tk.Frame, idx: int, bg: str):
        try:
            row.config(bg=bg)
        except tk.TclError:
            return
        children = row.winfo_children()
        for child in children:
            try:
                child.config(bg=bg)
                for gc in child.winfo_children():
                    try:
                        gc.config(bg=bg)
                    except tk.TclError:
                        pass
            except tk.TclError:
                pass
        # Update position label text/color (first child of row)
        if children:
            try:
                is_playing = (idx == self._browser._queue_index)
                stopped = self._browser._media_player._stopped
                paused = self._browser._media_player._audio_paused
                if is_playing and stopped:
                    icon, fg = "■", SUBTEXT_COLOR
                elif is_playing and paused:
                    icon, fg = "▌▌", ACCENT_COLOR
                elif is_playing:
                    icon, fg = "▶", ACCENT_COLOR
                else:
                    icon, fg = str(idx + 1), SUBTEXT_COLOR
                children[0].config(text=icon, fg=fg)
            except tk.TclError:
                pass

    def _on_enter(self, row: tk.Frame, idx: int):
        if idx not in self._selected and idx != self._browser._queue_index:
            self._recolor_row(row, idx, HOVER_BG)

    def _on_leave(self, row: tk.Frame, idx: int):
        if idx not in self._selected and idx != self._browser._queue_index:
            queue = self._browser._queue
            cut_marks = getattr(self._browser, '_queue_cut_marks', None)
            is_cut = (
                cut_marks is not None
                and 0 <= idx < len(queue)
                and any(ci == idx and cs is queue[idx] for ci, cs in cut_marks)
            )
            self._recolor_row(row, idx, _CUT_BG if is_cut else ITEM_BG)

    def _on_mousewheel(self, event: tk.Event):
        self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
