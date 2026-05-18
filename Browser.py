"""
Beat Saber Custom Song Browser
Parses Steam library to locate Beat Saber, then lists all custom songs
with cover art and metadata. Click art or title to select a song.
"""

import os
import re
import io
import json
import base64
import shutil
import webbrowser
import datetime
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from PIL import Image, ImageTk

from libraries.constants import (
    BG_COLOR, ACCENT_COLOR, TEXT_COLOR, SUBTEXT_COLOR,
    SELECTED_BG, HOVER_BG, ITEM_BG, SEPARATOR_COLOR, SCROLLBAR_BG,
    THUMBNAIL_SIZE, WINDOW_TITLE,
)
from libraries.steam_paths import find_beatsaber_custom_levels
from libraries.song_data import SongInfo, load_songs, load_song_hashes
from libraries.player_data import (
    DIFF_LABELS, RANK_LABELS, RANK_COLORS,
    DiffStat, find_player_data,
    load_favorites, load_player_stats,
    song_level_ids, get_song_stats, format_diff_stats,
)
from libraries.asset_editor import bak_files
from libraries.media_player import MediaPlayer
from libraries.favorites import add_to_favorites, remove_from_favorites
from libraries.install_manager import InstallManager
from libraries.song_operations import (
    restore_song_files, replace_song_art, replace_song_audio, clear_song_score,
)

PAGE_SIZE = 50

_QUEUE_THUMB = (48, 48)
_QUEUE_PLAYING_BG = "#1a1a3a"


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
        self._row_frames: list[tk.Frame] = []
        self._tick_id: str | None = None
        self._last_queue_len: int = -1
        self._last_queue_index: int = -2

        self.title("Playback Queue")
        self.configure(bg="#0d0d1a")
        self.geometry("420x500")
        self.minsize(340, 200)

        header = tk.Frame(self, bg="#0d0d1a")
        header.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(
            header, text="Queue",
            font=("Segoe UI", 13, "bold"),
            bg="#0d0d1a", fg=TEXT_COLOR,
        ).pack(side="left")
        tk.Label(
            header, text="  drag to reorder  •  shift+click to select  •  del to remove",
            font=("Segoe UI", 8),
            bg="#0d0d1a", fg=SUBTEXT_COLOR,
        ).pack(side="left")

        container = tk.Frame(self, bg="#0d0d1a")
        container.pack(fill="both", expand=True)

        self._canvas = tk.Canvas(container, bg="#0d0d1a", highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=self._canvas.yview)
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

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.refresh()
        self._tick_id = self.after(300, self._tick)

    def _on_close(self):
        if self._tick_id:
            self.after_cancel(self._tick_id)
        self._browser._queue_window = None
        self.destroy()

    def _tick(self):
        new_len = len(self._browser._queue)
        new_idx = self._browser._queue_index
        if new_len != self._last_queue_len:
            self.refresh()
        elif new_idx != self._last_queue_index:
            self._update_row_colors()
            self._last_queue_index = new_idx
        self._tick_id = self.after(300, self._tick)

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
            return

        for i, song in enumerate(queue):
            self._build_row(i, song)

    def _row_bg(self, idx: int) -> str:
        if idx == self._browser._queue_index:
            return _QUEUE_PLAYING_BG
        if idx in self._selected:
            return SELECTED_BG
        return ITEM_BG

    def _build_row(self, idx: int, song: "SongInfo"):
        bg = self._row_bg(idx)
        is_playing = (idx == self._browser._queue_index)

        row = tk.Frame(self._list_frame, bg=bg, cursor="fleur")
        row.pack(fill="x")
        self._row_frames.append(row)

        num_lbl = tk.Label(
            row,
            text="▶" if is_playing else str(idx + 1),
            font=("Segoe UI", 9, "bold"),
            bg=bg, fg=ACCENT_COLOR if is_playing else SUBTEXT_COLOR,
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
        if song.author_line:
            tk.Label(
                text_frame, text=song.author_line,
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

    def _on_right_click(self, event: tk.Event, idx: int, song: "SongInfo"):
        menu = tk.Menu(
            self, tearoff=0,
            bg="#1e1e1e", fg=TEXT_COLOR,
            activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR, bd=0,
        )
        menu.add_command(label="View Song", command=lambda: self._view_song(song))
        menu.add_command(label="Play",      command=lambda: self._play_from_queue(idx, song))
        menu.add_separator()
        menu.add_command(
            label="Save Queue",
            state="normal" if self._browser._queue else "disabled",
            command=lambda: self._browser._share_playlist(list(self._browser._queue)),
        )
        menu.tk_popup(event.x_root, event.y_root)

    def _view_song(self, song: "SongInfo"):
        b = self._browser
        idx = next((i for i, s in enumerate(b.filtered) if s is song), None)
        if idx is None:
            b.search_var.set("")
            idx = next((i for i, s in enumerate(b.filtered) if s is song), None)
            if idx is None:
                return
        b.page = idx // b.page_size
        b.selected_indices = {idx}
        b.selected_index = idx
        b._selected_folders = {str(song.folder)}
        b._render_list()
        b.status_bar.config(text=f"Selected: {song.display_name}")
        b.lift()
        b.focus_force()

    def _play_from_queue(self, idx: int, song: "SongInfo"):
        self._browser._queue_index = idx
        self._browser._play_audio(song)

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
        try:
            placeholder = Image.new("RGB", _QUEUE_THUMB, color="#2a0033")
            photo = ImageTk.PhotoImage(placeholder)
            self._thumbnails[key] = photo
            return photo
        except Exception:
            return self._browser._make_placeholder()

    # ── Drag-to-Reorder ──────────────────────────────────────────────────────

    def _on_press(self, event: tk.Event, idx: int):
        self._drag_source = idx
        self._drag_start_y = event.y_root
        self._dragging = False
        self._drag_target = None

    def _on_b1_motion(self, event: tk.Event, source_idx: int):
        if abs(event.y_root - self._drag_start_y) > 5 and not self._dragging:
            self._dragging = True
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
        playing_song = queue[curr] if 0 <= curr < len(queue) else None

        song = queue.pop(src)
        if dst > src:
            dst -= 1
        queue.insert(dst, song)

        if playing_song is not None:
            self._browser._queue_index = next(
                (i for i, s in enumerate(queue) if s is playing_song), -1
            )
        self.refresh()

    # ── Selection & Delete ───────────────────────────────────────────────────

    def _on_click(self, event: tk.Event, idx: int):
        if event.state & 0x1:
            if idx in self._selected:
                self._selected.discard(idx)
            else:
                self._selected.add(idx)
        else:
            self._selected = {idx}
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

    # ── Row Coloring ─────────────────────────────────────────────────────────

    def _update_row_colors(self):
        playing_idx = self._browser._queue_index
        for i, row in enumerate(self._row_frames):
            if self._dragging and i == self._drag_source:
                continue
            elif i == playing_idx:
                bg = _QUEUE_PLAYING_BG
            elif i in self._selected:
                bg = SELECTED_BG
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
                children[0].config(
                    text="▶" if is_playing else str(idx + 1),
                    fg=ACCENT_COLOR if is_playing else SUBTEXT_COLOR,
                )
            except tk.TclError:
                pass

    def _on_enter(self, row: tk.Frame, idx: int):
        if idx not in self._selected and idx != self._browser._queue_index:
            self._recolor_row(row, idx, HOVER_BG)

    def _on_leave(self, row: tk.Frame, idx: int):
        if idx not in self._selected and idx != self._browser._queue_index:
            self._recolor_row(row, idx, ITEM_BG)

    def _on_mousewheel(self, event: tk.Event):
        self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


class SongBrowser(tk.Tk):
    def __init__(self, custom_levels: Path):
        super().__init__()
        self.custom_levels = custom_levels
        self.songs: list[SongInfo] = []
        self.filtered: list[SongInfo] = []
        self.selected_index: int | None = None
        self.selected_indices: set[int] = set()
        self._selected_folders: set[str] = set()
        self._thumbnails: dict[str, ImageTk.PhotoImage] = {}   # keep refs alive; keyed by folder path
        self._placeholder: ImageTk.PhotoImage | None = None
        self._row_frames: list[tk.Frame] = []
        self._pending_install_id: str | None = None
        self.page: int = 0
        self.page_size: int = PAGE_SIZE

        self.player_stats: dict = {}
        self.favorite_ids: set[str] = set()
        self.player_dat_path: Path | None = None
        player_dat, pd_debug = find_player_data()
        if player_dat:
            self.player_dat_path = player_dat
            self.player_stats = load_player_stats(player_dat)
            self.favorite_ids = load_favorites(player_dat)
            self.player_data_status = f"PlayerData: {len(self.player_stats)} entries  |  {player_dat.name}"
        else:
            self.player_data_status = f"PlayerData not found: {pd_debug}"

        self.title(WINDOW_TITLE)
        self.configure(bg=BG_COLOR)
        self.geometry("780x680")
        self.minsize(600, 400)

        self._build_ui()

        self._media_player = MediaPlayer()
        self._media_player.start_media_keys(self.after, self._stop_player)
        self._queue: list[SongInfo] = []
        self._queue_index: int = -1
        self._player_bar_visible: bool = False
        self._queue_window: QueueWindow | None = None

        self._install_manager = InstallManager(
            custom_levels,
            self.after,
            lambda text: self.status_bar.config(text=text),
            self._on_install_complete_reload,
        )

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._load_async()

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Header
        header = tk.Frame(self, bg=BG_COLOR)
        header.pack(fill="x", padx=16, pady=(14, 4))

        tk.Label(
            header, text="🎵  Custom Songs",
            font=("Segoe UI", 18, "bold"),
            bg=BG_COLOR, fg=ACCENT_COLOR
        ).pack(side="left")

        self.count_label = tk.Label(
            header, text="",
            font=("Segoe UI", 10),
            bg=BG_COLOR, fg=SUBTEXT_COLOR
        )
        self.count_label.pack(side="left", padx=10, pady=4)

        # Search bar
        search_frame = tk.Frame(self, bg=BG_COLOR)
        search_frame.pack(fill="x", padx=16, pady=(0, 8))

        tk.Label(search_frame, text="🔍", bg=BG_COLOR, fg=SUBTEXT_COLOR,
                 font=("Segoe UI", 11)).pack(side="left")

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self._on_search)

        search_entry = tk.Entry(
            search_frame,
            textvariable=self.search_var,
            font=("Segoe UI", 11),
            bg="#1e1e1e", fg=TEXT_COLOR,
            insertbackground=TEXT_COLOR,
            relief="flat",
            bd=6,
        )
        search_entry.pack(side="left", fill="x", expand=True, ipady=4)
        search_entry.bind("<Return>", self._on_search_enter)
        self.search_entry = search_entry

        # Path label
        path_label = tk.Label(
            self,
            text=f"📂  {self.custom_levels}",
            font=("Segoe UI", 8),
            bg=BG_COLOR, fg="#555555",
            anchor="w",
        )
        path_label.pack(fill="x", padx=16, pady=(0, 6))

        # Scrollable song list
        container = tk.Frame(self, bg=BG_COLOR)
        container.pack(fill="both", expand=True, padx=16, pady=(0, 10))

        self.canvas = tk.Canvas(container, bg=BG_COLOR, highlightthickness=0)
        scrollbar = tk.Scrollbar(container, orient="vertical",
                                 command=self.canvas.yview,
                                 bg=SCROLLBAR_BG)
        self.canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.list_frame = tk.Frame(self.canvas, bg=BG_COLOR)
        self.canvas_window = self.canvas.create_window(
            (0, 0), window=self.list_frame, anchor="nw"
        )

        self.list_frame.bind("<Configure>", self._on_frame_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.bind("<F5>", self._refresh)
        self.bind("<space>", self._on_space)

        # Pagination controls
        self.pagination_frame = tk.Frame(self, bg=BG_COLOR)
        self.pagination_frame.pack(fill="x", padx=16, pady=(0, 4))

        self._prev_btn = tk.Button(
            self.pagination_frame, text="◀  Prev",
            font=("Segoe UI", 9),
            bg="#1e1e1e", fg=TEXT_COLOR,
            activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR,
            relief="flat", bd=4,
            command=self._prev_page,
        )
        self._prev_btn.pack(side="left")
        self._prev_btn.bind("<Button-3>", lambda _: self._jump_to_page())

        self._page_label = tk.Label(
            self.pagination_frame, text="",
            font=("Segoe UI", 9),
            bg=BG_COLOR, fg=SUBTEXT_COLOR,
        )
        self._page_label.pack(side="left", expand=True)
        self._page_label.bind("<Button-3>", lambda _: self._change_page_size())

        self._next_btn = tk.Button(
            self.pagination_frame, text="Next  ▶",
            font=("Segoe UI", 9),
            bg="#1e1e1e", fg=TEXT_COLOR,
            activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR,
            relief="flat", bd=4,
            command=self._next_page,
        )
        self._next_btn.pack(side="right")
        self._next_btn.bind("<Button-3>", lambda _: self._jump_to_page())

        # Now-playing bar (hidden until a song plays)
        self._player_bar_frame = tk.Frame(self, bg="#0d0d1a")
        # _player_bar_frame is shown/hidden dynamically — not packed here

        player_top = tk.Frame(self._player_bar_frame, bg="#0d0d1a")
        player_top.pack(fill="x", padx=10, pady=(5, 2))

        self._player_name_label = tk.Label(
            player_top, text="",
            font=("Segoe UI", 9, "bold"),
            bg="#0d0d1a", fg=ACCENT_COLOR,
            anchor="w",
        )
        self._player_name_label.pack(side="left", fill="x", expand=True)

        self._player_time_label = tk.Label(
            player_top, text="",
            font=("Segoe UI", 9),
            bg="#0d0d1a", fg=SUBTEXT_COLOR,
            anchor="e",
        )
        self._player_time_label.pack(side="right")

        style = ttk.Style()
        style.theme_use("default")
        style.configure(
            "Player.Horizontal.TProgressbar",
            troughcolor="#1a1a2e",
            background=ACCENT_COLOR,
            bordercolor="#0d0d1a",
            lightcolor=ACCENT_COLOR,
            darkcolor=ACCENT_COLOR,
        )
        self._player_progress = ttk.Progressbar(
            self._player_bar_frame,
            style="Player.Horizontal.TProgressbar",
            orient="horizontal",
            mode="determinate",
            maximum=100,
        )
        self._player_progress.pack(fill="x", padx=10, pady=(0, 5))

        for _w in (
            self._player_bar_frame,
            self._player_name_label,
            self._player_time_label,
            self._player_progress,
        ):
            _w.bind("<Button-3>", self._show_player_context_menu)

        self._player_tick_id: str | None = None

        # Status / selection bar
        self.status_bar = tk.Label(
            self,
            text="Loading songs…",
            font=("Segoe UI", 9),
            bg="#0a0a0a", fg=SUBTEXT_COLOR,
            anchor="w",
            pady=4,
        )
        self.status_bar.pack(fill="x", padx=16, pady=(0, 6))

    # ── Song loading ──────────────────────────────────────────────────────────

    def _refresh(self, *_):
        if self.player_dat_path:
            self.player_stats  = load_player_stats(self.player_dat_path)
            self.favorite_ids  = load_favorites(self.player_dat_path)
        self.status_bar.config(text="Refreshing…")
        self._load_async()

    def _load_async(self):
        def worker():
            songs = load_songs(self.custom_levels)
            hashes = load_song_hashes(self.custom_levels)
            for song in songs:
                song.song_hash = hashes.get(song.folder.name, "")
            self.after(0, lambda: self._on_loaded(songs))

        threading.Thread(target=worker, daemon=True).start()

    def _on_loaded(self, songs: list[SongInfo]):
        self.songs = songs
        self.filtered = songs[:]
        self.page = 0
        self._selected_folders.clear()
        self.selected_indices.clear()
        self.selected_index = None
        self.count_label.config(text=f"({len(songs)} songs)")
        self.status_bar.config(text=f"{len(songs)} songs found  •  {self.player_data_status}")
        self._render_list()

    # ── List rendering ────────────────────────────────────────────────────────

    def _make_placeholder(self) -> ImageTk.PhotoImage:
        if self._placeholder is None:
            img = Image.new("RGB", THUMBNAIL_SIZE, color="#2a0033")
            # Draw a simple music note shape using pixel art
            px = img.load()
            cx, cy = THUMBNAIL_SIZE[0] // 2, THUMBNAIL_SIZE[1] // 2
            for dy in range(-15, 16):
                for dx in range(-2, 3):
                    x, y = cx + dx, cy + dy - 5
                    if 0 <= x < THUMBNAIL_SIZE[0] and 0 <= y < THUMBNAIL_SIZE[1]:
                        px[x, y] = (199, 36, 177)
            for dx in range(0, 14):
                for dy in range(-2, 3):
                    x, y = cx + dx, cy - 20 + dy
                    if 0 <= x < THUMBNAIL_SIZE[0] and 0 <= y < THUMBNAIL_SIZE[1]:
                        px[x, y] = (199, 36, 177)
            self._placeholder = ImageTk.PhotoImage(img)
        return self._placeholder

    def _load_thumbnail(self, song: SongInfo) -> ImageTk.PhotoImage:
        key = str(song.folder)
        if key in self._thumbnails:
            return self._thumbnails[key]
        try:
            if song.cover_path:
                img = Image.open(song.cover_path).convert("RGB")
                img = img.resize(THUMBNAIL_SIZE, Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                self._thumbnails[key] = photo
                return photo
        except Exception:
            pass
        return self._make_placeholder()

    def _render_list(self):
        for w in self.list_frame.winfo_children():
            w.destroy()
        self._row_frames.clear()

        if self._pending_install_id:
            self._build_install_row(self._pending_install_id)

        page_start = self.page * self.page_size
        for local_idx, song in enumerate(self.filtered[page_start:page_start + self.page_size]):
            self._build_row(page_start + local_idx, song)

        for global_i in self.selected_indices:
            local_i = global_i - page_start
            if 0 <= local_i < len(self._row_frames):
                self._recolor_row(self._row_frames[local_i], SELECTED_BG)

        self._update_pagination_controls()
        self._update_scroll()

    def _is_favorite(self, song: SongInfo) -> bool:
        return any(lid in self.favorite_ids for lid in song_level_ids(song))

    def _build_row(self, idx: int, song: SongInfo):
        # Row container
        row = tk.Frame(self.list_frame, bg=ITEM_BG, cursor="hand2")
        row.pack(fill="x", pady=1)
        self._row_frames.append(row)

        # Thumbnail (loaded lazily)
        thumb_img = self._load_thumbnail(song)
        thumb_lbl = tk.Label(row, image=thumb_img, bg=ITEM_BG, cursor="hand2")
        thumb_lbl.image = thumb_img   # keep ref
        thumb_lbl.pack(side="left", padx=8, pady=6)

        # Text block
        text_frame = tk.Frame(row, bg=ITEM_BG)
        text_frame.pack(side="left", fill="both", expand=True, padx=4, pady=6)

        title_lbl = tk.Label(
            text_frame,
            text=song.display_name,
            font=("Segoe UI", 11, "bold"),
            bg=ITEM_BG, fg=TEXT_COLOR,
            anchor="w", cursor="hand2",
        )
        title_lbl.pack(fill="x")

        if song.author_line:
            author_lbl = tk.Label(
                text_frame,
                text=song.author_line,
                font=("Segoe UI", 9),
                bg=ITEM_BG, fg=SUBTEXT_COLOR,
                anchor="w",
            )
            author_lbl.pack(fill="x")

        meta_parts = []
        if song.bpm_str:
            meta_parts.append(song.bpm_str)
        if song.song_id:
            meta_parts.append(f"ID: {song.song_id}")
        added_str = datetime.datetime.fromtimestamp(song.created_at).strftime("Added %b %d, %Y")
        meta_parts.append(added_str)

        if meta_parts:
            meta_lbl = tk.Label(
                text_frame,
                text="  •  ".join(meta_parts),
                font=("Segoe UI", 8),
                bg=ITEM_BG, fg="#666666",
                anchor="w",
            )
            meta_lbl.pack(fill="x")

        # Player stats lines
        diff_stats = get_song_stats(song, self.player_stats)
        is_fav = self._is_favorite(song)
        if diff_stats:
            diff_parts, plays_line = format_diff_stats(diff_stats, song.diff_labels)
            if diff_parts:
                scores_frame = tk.Frame(text_frame, bg=ITEM_BG)
                scores_frame.pack(fill="x")
                for i, (text, is_fc) in enumerate(diff_parts):
                    if i > 0:
                        tk.Label(scores_frame, text="  •  ", font=("Courier New", 8),
                                 bg=ITEM_BG, fg=SUBTEXT_COLOR).pack(side="left")
                    tk.Label(scores_frame, text=text, font=("Courier New", 8),
                             bg=ITEM_BG, fg=ACCENT_COLOR if is_fc else TEXT_COLOR,
                             anchor="w").pack(side="left")
            if plays_line:
                display_plays = ("★ " + plays_line) if is_fav else plays_line
                plays_lbl = tk.Label(
                    text_frame,
                    text=display_plays,
                    font=("Segoe UI", 8),
                    bg=ITEM_BG, fg="#FFD700" if is_fav else "#888888",
                    anchor="w",
                )
                plays_lbl.pack(fill="x")
        else:
            tk.Label(
                text_frame,
                text="Easy:0 | DNF",
                font=("Courier New", 8),
                bg=ITEM_BG, fg="#555555",
                anchor="w",
            ).pack(fill="x")
            tk.Label(
                text_frame,
                text=("★ Plays: 0") if is_fav else "Plays: 0",
                font=("Segoe UI", 8),
                bg=ITEM_BG, fg="#FFD700" if is_fav else "#555555",
                anchor="w",
            ).pack(fill="x")

        # Separator
        sep = tk.Frame(self.list_frame, bg=SEPARATOR_COLOR, height=1)
        sep.pack(fill="x")

        # Bind click / hover to all widgets in the row
        widgets = [row, thumb_lbl, text_frame, title_lbl]
        for child in text_frame.winfo_children():
            widgets.append(child)
            for grandchild in child.winfo_children():
                widgets.append(grandchild)

        for w in widgets:
            w.bind("<Button-1>",         lambda e, i=idx: self._select(i, shift_held=bool(e.state & 0x1)))
            w.bind("<Control-Button-1>",   lambda _, s=song: webbrowser.open(f"https://beatsaver.com/maps/{s.song_id}") if s.song_id else None)
            w.bind("<Button-3>",         lambda e, i=idx, s=song: self._on_right_click(e, i, s))
            w.bind("<Enter>",       lambda e, r=row, s=sep: self._hover(r, s, True))
            w.bind("<Leave>",       lambda e, r=row, s=sep: self._hover(r, s, False))
            w.bind("<MouseWheel>",  self._on_mousewheel)

    def _on_close(self):
        self._install_manager.cancel()
        self._media_player.stop_listener()
        self._media_player.stop()
        self.destroy()

    def _on_space(self, *_):
        if self.focus_get() is self.search_entry:
            return
        self._media_player.toggle_pause()

    # ── Favorites ─────────────────────────────────────────────────────────────

    def _add_to_favorites(self, song: SongInfo):
        if not self.player_dat_path:
            return
        if add_to_favorites(self.player_dat_path, song, self.favorite_ids):
            self._render_list()

    def _remove_from_favorites(self, song: SongInfo):
        if not self.player_dat_path:
            return
        if remove_from_favorites(self.player_dat_path, song, self.favorite_ids):
            self._render_list()

    def _add_to_favorites_multi(self, songs: list[SongInfo]):
        if not self.player_dat_path:
            return
        changed = False
        for song in songs:
            if not self._is_favorite(song):
                if add_to_favorites(self.player_dat_path, song, self.favorite_ids):
                    changed = True
        if changed:
            self._render_list()

    def _remove_from_favorites_multi(self, songs: list[SongInfo]):
        if not self.player_dat_path:
            return
        changed = False
        for song in songs:
            if self._is_favorite(song):
                if remove_from_favorites(self.player_dat_path, song, self.favorite_ids):
                    changed = True
        if changed:
            self._render_list()

    def _share_playlist(self, songs: list[SongInfo]) -> None:
        invalid = [s for s in songs if not s.song_hash]
        valid = [s for s in songs if s.song_hash]

        if invalid:
            names = "\n".join(f"  • {s.display_name}" for s in invalid)
            if not valid:
                messagebox.showerror(
                    "Cannot Create Playlist",
                    "None of the selected songs have a hash — they may not have been "
                    "loaded by Beat Saber yet.\n\n" + names,
                )
                return
            proceed = messagebox.askyesno(
                "Invalid Songs",
                f"{len(invalid)} song(s) have no hash and will be skipped:\n\n"
                + names
                + "\n\nContinue with the remaining "
                + str(len(valid))
                + " song(s)?",
            )
            if not proceed:
                return

        import tkinter.filedialog as fd
        save_path = fd.asksaveasfilename(
            title="Save Playlist",
            filetypes=[("Beat Saber Playlist", "*.bplist"), ("All files", "*.*")],
            defaultextension=".bplist",
        )
        if not save_path:
            return

        title = Path(save_path).stem

        image_data = ""
        for song in valid:
            if song.cover_path and song.cover_path.exists():
                try:
                    buf = io.BytesIO()
                    Image.open(song.cover_path).convert("RGB").save(buf, format="JPEG")
                    image_data = base64.b64encode(buf.getvalue()).decode("ascii")
                except Exception:
                    pass
                break

        playlist = {
            "playlistTitle": title,
            "playlistAuthor": "",
            "image": image_data,
            "customData": {},
            "songs": [
                {"key": s.song_id, "hash": s.song_hash, "songName": s.display_name}
                for s in valid
            ],
        }

        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(playlist, f, indent=2)

        messagebox.showinfo(
            "Playlist Saved",
            f"Saved {len(valid)} songs to {Path(save_path).name}",
        )

    # ── Song operations ───────────────────────────────────────────────────────

    def _play_audio(self, song: SongInfo):
        if song is not self._media_player.playing_song:
            self._media_player._looping = False
        self._media_player.play(song)
        self._show_player_bar(song)
        self._start_player_tick()

    def _play_queue(self, songs: list[SongInfo]) -> None:
        playable = [s for s in songs if s.audio_path]
        if not playable:
            return
        self._queue = playable
        self._queue_index = 0
        self._play_audio(playable[0])
        self._notify_queue_window()

    def _add_to_queue(self, songs: list[SongInfo]) -> None:
        playable = [s for s in songs if s.audio_path]
        if not playable:
            return
        self._queue.extend(playable)
        if self._media_player.playing_song is None and self._player_bar_visible:
            self._queue_index = len(self._queue) - len(playable)
            self._play_audio(self._queue[self._queue_index])
        self._notify_queue_window()

    def _queue_next(self) -> None:
        if self._media_player._looping:
            return
        next_idx = self._queue_index + 1
        if next_idx < len(self._queue):
            self._queue_index = next_idx
            self._play_audio(self._queue[next_idx])

    def _queue_prev(self) -> None:
        if self._media_player._looping:
            return
        if self._queue_index > 0:
            self._queue_index -= 1
            self._play_audio(self._queue[self._queue_index])

    def _show_player_bar_idle(self, song: SongInfo | None, duration: float | None) -> None:
        name = (song.display_name or song.song_name or "Unknown") if song else "Unknown"
        self._player_name_label.config(text=f"⏹  {name}")
        if duration:
            d_min, d_sec = divmod(int(duration), 60)
            self._player_time_label.config(text=f"{d_min}:{d_sec:02d} / {d_min}:{d_sec:02d}")
            self._player_progress["value"] = 100.0
        else:
            self._player_time_label.config(text="--:--")

    def _show_player_bar(self, song: SongInfo):
        name = song.display_name or song.song_name or "Unknown"
        loop_suffix = " 🔁" if self._media_player._looping else ""
        self._player_name_label.config(text=f"▶  {name}{loop_suffix}")
        self._player_time_label.config(text="0:00")
        self._player_progress["value"] = 0
        self._player_bar_frame.pack(fill="x", padx=16, pady=(0, 4), before=self.status_bar)
        self._player_bar_visible = True

    def _hide_player_bar(self):
        self._player_bar_frame.pack_forget()
        self._player_bar_visible = False

    def _stop_player(self):
        if self._player_tick_id:
            self.after_cancel(self._player_tick_id)
            self._player_tick_id = None
        self._media_player.stop()
        self._hide_player_bar()
        self._queue.clear()
        self._queue_index = -1
        self._notify_queue_window()

    def _stop_audio_keep_queue(self):
        if self._player_tick_id:
            self.after_cancel(self._player_tick_id)
            self._player_tick_id = None
        self._media_player.stop()
        self._hide_player_bar()
        self._queue_index = -1

    def _show_player_context_menu(self, event: tk.Event):
        mp = self._media_player
        paused = mp._audio_paused
        play_label = "Pause" if not paused else "Play"

        loop_var = tk.BooleanVar(value=mp._looping)

        menu = tk.Menu(self, tearoff=0, bg="#1e1e1e", fg=TEXT_COLOR,
                       activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR, bd=0)
        menu.add_command(label=play_label, command=mp.toggle_pause)
        menu.add_command(label="Stop", command=self._stop_player)
        menu.add_checkbutton(
            label="Loop", variable=loop_var, command=mp.toggle_loop,
            selectcolor=ACCENT_COLOR,
        )
        can_next = not mp._looping and (self._queue_index + 1 < len(self._queue))
        can_prev = not mp._looping and (self._queue_index > 0)
        menu.add_separator()
        menu.add_command(label="Next",
                         state="normal" if can_next else "disabled",
                         command=self._queue_next)
        menu.add_command(label="Previous",
                         state="normal" if can_prev else "disabled",
                         command=self._queue_prev)
        menu.add_separator()
        menu.add_command(label="View Queue",
                         state="normal" if self._queue else "disabled",
                         command=self._open_queue_window)
        menu.tk_popup(event.x_root, event.y_root)

    def _open_queue_window(self):
        if self._queue_window and self._queue_window.winfo_exists():
            self._queue_window.lift()
            self._queue_window.focus_force()
            return
        self._queue_window = QueueWindow(self)

    def _notify_queue_window(self):
        if self._queue_window and self._queue_window.winfo_exists():
            self._queue_window.refresh()

    def _start_player_tick(self):
        if self._player_tick_id:
            self.after_cancel(self._player_tick_id)
        self._player_tick_id = self.after(500, self._tick_player)

    def _tick_player(self):
        proc = self._media_player._audio_proc
        if proc is None or proc.poll() is not None:
            if self._media_player._looping and self._media_player.playing_song:
                self._play_audio(self._media_player.playing_song)
                return
            next_idx = self._queue_index + 1
            if 0 <= next_idx < len(self._queue):
                self._queue_index = next_idx
                self._play_audio(self._queue[next_idx])
                return
            last_song = self._media_player.playing_song
            last_duration = self._media_player.song_duration
            self._media_player.stop()
            self._show_player_bar_idle(last_song, last_duration)
            self._player_tick_id = None
            return

        elapsed = self._media_player.elapsed_seconds() or 0.0
        duration = self._media_player.song_duration
        paused = self._media_player._audio_paused

        icon = "⏸" if paused else "▶"
        song = self._media_player.playing_song
        name = (song.display_name or song.song_name or "Unknown") if song else ""
        loop_suffix = " 🔁" if self._media_player._looping else ""
        self._player_name_label.config(text=f"{icon}  {name}{loop_suffix}")

        e_min, e_sec = divmod(int(elapsed), 60)
        if duration:
            d_min, d_sec = divmod(int(duration), 60)
            self._player_time_label.config(text=f"{e_min}:{e_sec:02d} / {d_min}:{d_sec:02d}")
            pct = min(100.0, elapsed / duration * 100)
            self._player_progress["value"] = pct
        else:
            self._player_time_label.config(text=f"{e_min}:{e_sec:02d}")
            self._player_progress["value"] = 0

        self._player_tick_id = self.after(500, self._tick_player)

    def _restore_files(self, song: SongInfo):
        count, errors = restore_song_files(song)
        if count == 0:
            return
        if errors:
            messagebox.showerror("Restore Failed", "\n".join(errors))
        else:
            self._thumbnails.clear()
            self._render_list()
            self.status_bar.config(text=f"Restored {count} file(s) for: {song.display_name}")

    def _replace_art(self, song: SongInfo):
        if replace_song_art(self, song):
            self._thumbnails.clear()
            self._render_list()

    def _replace_audio(self, song: SongInfo):
        if replace_song_audio(self, song):
            self.status_bar.config(text=f"Audio replaced for: {song.display_name}")

    def _show_context_menu(self, event: tk.Event, song: SongInfo):
        is_fav = self._is_favorite(song)
        baks = bak_files(song)
        menu = tk.Menu(self, tearoff=0, bg="#1e1e1e", fg=TEXT_COLOR,
                       activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR,
                       bd=0)
        if self._player_bar_visible:
            menu.add_command(label="Add to Queue",
                             command=lambda: self._add_to_queue([song]),
                             state="normal" if song.audio_path else "disabled")
        else:
            menu.add_command(label="Play Audio",
                             command=lambda: self._play_queue([song]),
                             state="normal" if song.audio_path else "disabled")
        if is_fav:
            menu.add_command(label="Remove from Favorites",
                             command=lambda: self._remove_from_favorites(song),
                             state="normal" if self.player_dat_path else "disabled")
        else:
            menu.add_command(label="Add to Favorites",
                             command=lambda: self._add_to_favorites(song),
                             state="normal" if self.player_dat_path else "disabled")
        menu.add_separator()
        menu.add_command(label="Replace Art",
                         command=lambda: self._replace_art(song),
                         state="normal" if song.cover_path else "disabled")
        menu.add_command(label="Replace Audio",
                         command=lambda: self._replace_audio(song),
                         state="normal" if song.audio_path else "disabled")
        if baks:
            menu.add_command(label=f"Restore Files ({len(baks)})",
                             command=lambda: self._restore_files(song))
        menu.add_separator()
        menu.add_command(label="Copy Link",
                         command=lambda: self._copy(f"https://beatsaver.com/maps/{song.song_id}"),
                         state="normal" if song.song_id else "disabled")
        menu.add_command(label="Copy Name", command=lambda: self._copy(song.display_name))
        menu.add_separator()
        menu.add_command(label="Open Folder…",
                         command=lambda: os.startfile(song.folder))
        menu.add_separator()
        shift_held = bool(event.state & 0x1)
        if shift_held:
            menu.add_command(label="Clear Score",
                             command=lambda: self._clear_score(song),
                             state="normal" if self.player_dat_path else "disabled")
        is_deletable = not is_fav or shift_held
        menu.add_command(label="Delete",
                         command=lambda: self._delete_song(song),
                         state="normal" if is_deletable else "disabled",
                         foreground="#ff5555" if is_deletable else SUBTEXT_COLOR)
        menu.tk_popup(event.x_root, event.y_root)

    def _on_right_click(self, event: tk.Event, _idx: int, song: SongInfo):
        if len(self._selected_folders) > 1 and str(song.folder) in self._selected_folders:
            songs = [s for s in self.songs if str(s.folder) in self._selected_folders]
            self._show_context_menu_multi(event, songs)
        else:
            self._show_context_menu(event, song)

    def _show_context_menu_multi(self, event: tk.Event, songs: list[SongInfo]):
        shift_held = bool(event.state & 0x1)
        any_favorited = any(self._is_favorite(s) for s in songs)
        fav_state = "normal" if self.player_dat_path else "disabled"
        menu = tk.Menu(self, tearoff=0, bg="#1e1e1e", fg=TEXT_COLOR,
                       activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR,
                       bd=0)
        if self._player_bar_visible:
            menu.add_command(label="Add to Queue",
                             command=lambda: self._add_to_queue(songs))
        else:
            has_audio = any(s.audio_path for s in songs)
            menu.add_command(label="Play",
                             state="normal" if has_audio else "disabled",
                             command=lambda: self._play_queue(songs))
        menu.add_separator()
        menu.add_command(label="Add to Favorites",
                         command=lambda: self._add_to_favorites_multi(songs),
                         state=fav_state)
        menu.add_command(label="Remove from Favorites",
                         command=lambda: self._remove_from_favorites_multi(songs),
                         state=fav_state)
        menu.add_separator()
        menu.add_command(label="Share Playlist",
                         command=lambda: self._share_playlist(songs),
                         state="normal")
        menu.add_separator()
        is_deletable = not any_favorited or shift_held
        menu.add_command(label="Delete",
                         command=lambda: self._delete_songs(songs, shift_held),
                         state="normal" if is_deletable else "disabled",
                         foreground="#ff5555" if is_deletable else SUBTEXT_COLOR)
        menu.tk_popup(event.x_root, event.y_root)

    def _copy(self, text: str):
        self.clipboard_clear()
        self.clipboard_append(text)

    def _clear_score(self, song: SongInfo):
        if not self.player_dat_path:
            return
        if not messagebox.askyesno(
            "Clear Score",
            f'Clear all scores for "{song.display_name}"?\n\nThis cannot be undone (a backup will be made).',
            icon="warning", default="no",
        ):
            return
        result = clear_song_score(self.player_dat_path, song)
        if result is not None:
            removed, new_stats = result
            self.player_stats = new_stats
            self._render_list()
            self.status_bar.config(
                text=f"Cleared {removed} score entr{'y' if removed == 1 else 'ies'} for: {song.display_name}"
            )

    def _delete_song(self, song: SongInfo):
        msg = f'Delete "{song.display_name}"?\n\nThe folder will be removed from CustomLevels. Your scores will not be affected.'
        if not messagebox.askyesno("Delete Song", msg, icon="warning", default="no"):
            return
        if song is self._media_player.playing_song:
            self._media_player.stop_and_wait()
        try:
            shutil.rmtree(song.folder)
        except Exception as exc:
            messagebox.showerror("Delete Failed", str(exc))
            return
        self.songs    = [s for s in self.songs    if s is not song]
        self.filtered = [s for s in self.filtered if s is not song]
        self._selected_folders.discard(str(song.folder))
        self.selected_indices = {
            i for i, s in enumerate(self.filtered)
            if str(s.folder) in self._selected_folders
        }
        self.selected_index = max(self.selected_indices) if self.selected_indices else None
        self._thumbnails.clear()
        self._render_list()
        self.count_label.config(text=f"({len(self.songs)} songs)")
        self.status_bar.config(text=f"{len(self.filtered)} songs shown")

    def _delete_songs(self, songs: list[SongInfo], shift_held: bool):
        if any(self._is_favorite(s) for s in songs) and not shift_held:
            return
        count = len(songs)
        msg = (f'Delete {count} songs?\n\n'
               f'The folders will be removed from CustomLevels. Your scores will not be affected.')
        if not messagebox.askyesno("Delete Songs", msg, icon="warning", default="no"):
            return
        failed: list[tuple[SongInfo, Exception]] = []
        for song in songs:
            if song is self._media_player.playing_song:
                self._media_player.stop_and_wait()
            try:
                shutil.rmtree(song.folder)
            except Exception as exc:
                failed.append((song, exc))
        deleted_ids = {id(s) for s in songs} - {id(s) for s, _ in failed}
        deleted_folders = {str(s.folder) for s in songs if id(s) in deleted_ids}
        self.songs    = [s for s in self.songs    if id(s) not in deleted_ids]
        self.filtered = [s for s in self.filtered if id(s) not in deleted_ids]
        self._selected_folders -= deleted_folders
        self.selected_indices = {
            i for i, s in enumerate(self.filtered)
            if str(s.folder) in self._selected_folders
        }
        self.selected_index = max(self.selected_indices) if self.selected_indices else None
        self._thumbnails.clear()
        self._render_list()
        self.count_label.config(text=f"({len(self.songs)} songs)")
        self.status_bar.config(text=f"{len(self.filtered)} songs shown")
        if failed:
            errs = "\n".join(f"{s.display_name}: {exc}" for s, exc in failed)
            messagebox.showerror("Delete Failed", f"Failed to delete {len(failed)} song(s):\n{errs}")

    def _hover(self, row: tk.Frame, sep: tk.Frame, entering: bool):
        if self._row_is_selected(row):
            return
        bg = HOVER_BG if entering else ITEM_BG
        self._recolor_row(row, bg)

    def _row_is_selected(self, row: tk.Frame) -> bool:
        return row.cget("bg") == SELECTED_BG

    def _recolor_row(self, row: tk.Frame, bg: str):
        row.configure(bg=bg)
        for child in row.winfo_children():
            try:
                child.configure(bg=bg)
            except Exception:
                pass
            for grandchild in child.winfo_children():
                try:
                    grandchild.configure(bg=bg)
                except Exception:
                    pass
                for great in grandchild.winfo_children():
                    try:
                        great.configure(bg=bg)
                    except Exception:
                        pass

    def _select(self, idx: int, shift_held: bool = False):
        self.canvas.focus_set()
        page_start = self.page * self.page_size
        local_idx = idx - page_start

        if shift_held:
            if idx in self.selected_indices:
                self.selected_indices.discard(idx)
                self._selected_folders.discard(str(self.filtered[idx].folder))
                if 0 <= local_idx < len(self._row_frames):
                    self._recolor_row(self._row_frames[local_idx], ITEM_BG)
                if self.selected_index == idx:
                    self.selected_index = max(self.selected_indices) if self.selected_indices else None
            else:
                self.selected_indices.add(idx)
                self._selected_folders.add(str(self.filtered[idx].folder))
                self.selected_index = idx
                if 0 <= local_idx < len(self._row_frames):
                    self._recolor_row(self._row_frames[local_idx], SELECTED_BG)
        else:
            for i in self.selected_indices:
                li = i - page_start
                if 0 <= li < len(self._row_frames):
                    self._recolor_row(self._row_frames[li], ITEM_BG)
            self.selected_indices = {idx}
            self._selected_folders = {str(self.filtered[idx].folder)}
            self.selected_index = idx
            if 0 <= local_idx < len(self._row_frames):
                self._recolor_row(self._row_frames[local_idx], SELECTED_BG)

        if len(self._selected_folders) > 1:
            self.status_bar.config(text=f"{len(self._selected_folders)} songs selected")
        elif self.selected_index is not None:
            song = self.filtered[self.selected_index]
            self.status_bar.config(
                text=f"Selected: {song.display_name}"
                     + (f"  •  {song.author}" if song.author else "")
                     + (f"  •  {song.bpm_str}" if song.bpm_str else "")
            )

    # ── Pagination ────────────────────────────────────────────────────────────

    def _change_page_size(self):
        dlg = tk.Toplevel(self, bg=BG_COLOR)
        dlg.title("Results per page")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        tk.Label(
            dlg, text="Results per page:",
            bg=BG_COLOR, fg=TEXT_COLOR, font=("Segoe UI", 10),
        ).pack(padx=20, pady=(16, 6))

        entry = tk.Entry(
            dlg, font=("Segoe UI", 10), width=10,
            bg="#1e1e1e", fg=TEXT_COLOR, insertbackground=TEXT_COLOR,
            relief="flat", bd=4, justify="center",
        )
        entry.insert(0, str(self.page_size))
        entry.select_range(0, "end")
        entry.pack(padx=20, pady=(0, 12))
        entry.focus_set()

        btn_frame = tk.Frame(dlg, bg=BG_COLOR)
        btn_frame.pack(padx=20, pady=(0, 16))

        result: list[int | None] = [None]

        def _confirm():
            try:
                result[0] = max(1, int(entry.get()))
            except ValueError:
                pass
            dlg.destroy()

        def _cancel():
            dlg.destroy()

        tk.Button(
            btn_frame, text="OK", width=8,
            bg="#1e1e1e", fg=TEXT_COLOR,
            activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR,
            relief="flat", bd=4, command=_confirm,
        ).pack(side="left", padx=(0, 8))

        tk.Button(
            btn_frame, text="Cancel", width=8,
            bg="#1e1e1e", fg=TEXT_COLOR,
            activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR,
            relief="flat", bd=4, command=_cancel,
        ).pack(side="left")

        entry.bind("<Return>", lambda _: _confirm())
        entry.bind("<Escape>", lambda _: _cancel())

        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{x}+{y}")

        self.wait_window(dlg)

        if result[0] is not None and result[0] != self.page_size:
            self.page_size = result[0]
            self.page = 0
            self._render_list()

    def _prev_page(self):
        if self.page > 0:
            self.page -= 1
            self._render_list()

    def _next_page(self):
        total_pages = max(1, (len(self.filtered) + self.page_size - 1) // self.page_size)
        if self.page < total_pages - 1:
            self.page += 1
            self._render_list()

    def _jump_to_page(self):
        total_pages = max(1, (len(self.filtered) + self.page_size - 1) // self.page_size)
        if total_pages <= 1:
            return

        dlg = tk.Toplevel(self, bg=BG_COLOR)
        dlg.title("Go to page")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        tk.Label(
            dlg, text=f"Enter page number (1–{total_pages}):",
            bg=BG_COLOR, fg=TEXT_COLOR, font=("Segoe UI", 10),
        ).pack(padx=20, pady=(16, 6))

        entry = tk.Entry(
            dlg, font=("Segoe UI", 10), width=10,
            bg="#1e1e1e", fg=TEXT_COLOR, insertbackground=TEXT_COLOR,
            relief="flat", bd=4, justify="center",
        )
        entry.insert(0, str(self.page + 1))
        entry.select_range(0, "end")
        entry.pack(padx=20, pady=(0, 12))
        entry.focus_set()

        btn_frame = tk.Frame(dlg, bg=BG_COLOR)
        btn_frame.pack(padx=20, pady=(0, 16))

        result: list[int | None] = [None]

        def _confirm():
            try:
                result[0] = int(entry.get())
            except ValueError:
                pass
            dlg.destroy()

        def _cancel():
            dlg.destroy()

        ok_btn = tk.Button(
            btn_frame, text="OK", width=8,
            bg="#1e1e1e", fg=TEXT_COLOR,
            activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR,
            relief="flat", bd=4, command=_confirm,
        )
        ok_btn.pack(side="left", padx=(0, 8))

        tk.Button(
            btn_frame, text="Cancel", width=8,
            bg="#1e1e1e", fg=TEXT_COLOR,
            activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR,
            relief="flat", bd=4, command=_cancel,
        ).pack(side="left")

        entry.bind("<Return>", lambda _: _confirm())
        entry.bind("<Escape>", lambda _: _cancel())

        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{x}+{y}")

        self.wait_window(dlg)

        if result[0] is not None:
            self.page = max(0, min(result[0] - 1, total_pages - 1))
            self._render_list()

    def _update_pagination_controls(self):
        total = len(self.filtered)
        total_pages = max(1, (total + self.page_size - 1) // self.page_size)
        self._prev_btn.config(state="normal" if self.page > 0 else "disabled")
        self._next_btn.config(state="normal" if self.page < total_pages - 1 else "disabled")
        if total == 0:
            self._page_label.config(text="")
        elif total_pages <= 1:
            self._page_label.config(text="")
        else:
            start = self.page * self.page_size + 1
            end = min(start + self.page_size - 1, total)
            self._page_label.config(
                text=f"Page {self.page + 1} of {total_pages}  •  {start}–{end} of {total}"
            )

    # ── Search ────────────────────────────────────────────────────────────────

    def _extract_song_id(self, query: str) -> str | None:
        """Return the BeatSaver song ID if query is a one-click or map URL, else None."""
        q = query.strip()
        m = re.match(r'^beatsaver://([A-Za-z0-9]+)/?$', q, re.IGNORECASE)
        if m:
            return m.group(1).lower()
        m = re.match(r'^https?://beatsaver\.com/maps/([A-Za-z0-9]+)', q, re.IGNORECASE)
        if m:
            return m.group(1).lower()
        return None

    def _on_search(self, *_):
        query = self.search_var.get().strip()

        song_id = self._extract_song_id(query)
        if song_id:
            installed = [s for s in self.songs if s.song_id.lower() == song_id]
            if installed:
                self.filtered = installed
                self._pending_install_id = None
                self.status_bar.config(text=f"Song {song_id} is already installed.")
            else:
                self.filtered = []
                self._pending_install_id = song_id
                self.status_bar.config(
                    text=f"Song {song_id} not installed — press Enter or click to install via Mod Assistant."
                )
            self.selected_indices = {
                i for i, s in enumerate(self.filtered)
                if str(s.folder) in self._selected_folders
            }
            self.selected_index = max(self.selected_indices) if self.selected_indices else None
            self._thumbnails.clear()
            self.page = 0
            self._render_list()
            return

        self._pending_install_id = None
        self._install_manager.cancel()
        query_lower = query.lower()
        if not query_lower:
            self.filtered = self.songs[:]
        else:
            self.filtered = [
                s for s in self.songs
                if query_lower in s.display_name.lower()
                or query_lower in s.author.lower()
                or query_lower in s.mapper.lower()
                or query_lower in s.song_id.lower()
            ]
        self.selected_indices = {
            i for i, s in enumerate(self.filtered)
            if str(s.folder) in self._selected_folders
        }
        self.selected_index = max(self.selected_indices) if self.selected_indices else None
        self._thumbnails.clear()   # free memory; will reload on render
        self.page = 0
        self._render_list()
        self.status_bar.config(text=f"{len(self.filtered)} songs shown")

    def _on_search_enter(self, *_):
        if self._pending_install_id:
            self._trigger_install(self._pending_install_id)

    def _trigger_install(self, song_id: str):
        self._install_manager.trigger(song_id)

    # ── Install completion ────────────────────────────────────────────────────

    def _on_install_complete_reload(self):
        def worker():
            songs = load_songs(self.custom_levels)
            hashes = load_song_hashes(self.custom_levels)
            for song in songs:
                song.song_hash = hashes.get(song.folder.name, "")
            self.after(0, lambda: self._after_install_load(songs))

        threading.Thread(target=worker, daemon=True).start()

    def _after_install_load(self, songs: list[SongInfo]):
        self.songs = songs
        self.count_label.config(text=f"({len(songs)} songs)")
        self._on_search()  # re-applies search bar content; shows installed song and clears pending_install_id

    def _build_install_row(self, song_id: str):
        row = tk.Frame(self.list_frame, bg=HOVER_BG, cursor="hand2")
        row.pack(fill="x", pady=1)

        icon_lbl = tk.Label(row, text="⬇", font=("Segoe UI", 20),
                            bg=HOVER_BG, fg=ACCENT_COLOR, width=4)
        icon_lbl.pack(side="left", padx=8, pady=6)

        text_frame = tk.Frame(row, bg=HOVER_BG)
        text_frame.pack(side="left", fill="both", expand=True, padx=4, pady=6)

        title_lbl = tk.Label(
            text_frame,
            text=f"Click to install {song_id}…",
            font=("Segoe UI", 11, "bold"),
            bg=HOVER_BG, fg=ACCENT_COLOR,
            anchor="w", cursor="hand2",
        )
        title_lbl.pack(fill="x")

        sub_lbl = tk.Label(
            text_frame,
            text="Opens one-click install via Mod Assistant  •  or press Enter",
            font=("Segoe UI", 9),
            bg=HOVER_BG, fg=SUBTEXT_COLOR,
            anchor="w",
        )
        sub_lbl.pack(fill="x")

        sep = tk.Frame(self.list_frame, bg=SEPARATOR_COLOR, height=1)
        sep.pack(fill="x")

        for w in [row, icon_lbl, text_frame, title_lbl, sub_lbl]:
            w.bind("<Button-1>",   lambda _, sid=song_id: self._trigger_install(sid))
            w.bind("<Enter>",      lambda _, r=row: self._recolor_row(r, SELECTED_BG))
            w.bind("<Leave>",      lambda _, r=row: self._recolor_row(r, HOVER_BG))
            w.bind("<MouseWheel>", self._on_mousewheel)

    # ── Scroll helpers ────────────────────────────────────────────────────────

    def _on_frame_configure(self, _):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfig(self.canvas_window, width=event.width)

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _update_scroll(self):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.canvas.yview_moveto(0)


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    # Try to find custom levels automatically
    custom_levels = find_beatsaber_custom_levels()

    if custom_levels is None:
        # Fallback: ask user
        import tkinter.filedialog as fd
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(
            "Beat Saber not found",
            "Could not locate Beat Saber automatically.\n"
            "Please select your CustomLevels folder manually.",
        )
        path_str = fd.askdirectory(title="Select CustomLevels folder")
        root.destroy()
        if not path_str:
            return
        custom_levels = Path(path_str)

    app = SongBrowser(custom_levels)
    app.mainloop()


if __name__ == "__main__":
    main()
