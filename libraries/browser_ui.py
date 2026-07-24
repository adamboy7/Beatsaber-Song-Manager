"""
UI construction and list rendering for SongBrowser.

Owns the menubar, the main window chrome (search bar, song-list
canvas, pagination controls, now-playing bar widgets, status bar),
and per-song row building / hover / selection bookkeeping.
"""

from __future__ import annotations

import datetime
import os
import webbrowser
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from PIL import Image, ImageTk

from libraries.constants import (
    BG_COLOR, ACCENT_COLOR, TEXT_COLOR, SUBTEXT_COLOR,
    SELECTED_BG, HOVER_BG, ITEM_BG, SEPARATOR_COLOR, SCROLLBAR_BG,
    THUMBNAIL_SIZE,
)
from libraries.song_data import SongInfo
from libraries.player_data import get_song_stats, format_diff_stats

THUMBNAIL_CACHE_LIMIT = 300


class BrowserUIMixin:
    """Menu/window construction, thumbnail caching, list rendering,
    row hover/selection. Methods here read/write the standard
    SongBrowser attributes (``self.filtered``, ``self.page``,
    ``self.selected_indices``, etc.)."""

    # ── Menus / chrome ────────────────────────────────────────────────────────

    def _add_folder_menu_items(self):
        custom_songs = self.custom_levels
        bs_install = custom_songs.parent.parent if custom_songs else None
        appdata = Path.home() / "AppData" / "LocalLow" / "Hyperbolic Magnetism" / "Beat Saber"

        def _open(p: Path):
            if os.name != "nt":
                return
            os.startfile(p)

        for label, path in (
            ("Open Custom Songs Folder", custom_songs),
            ("Open Beat Saber AppData",  appdata),
            ("Open Beat Saber Folder",   bs_install),
        ):
            valid = path is not None and path.is_dir()
            self._file_menu.add_command(
                label=label,
                command=(lambda p=path: _open(p)) if valid else None,
                state="normal" if valid else "disabled",
            )

    def _build_menubar(self):
        menubar = tk.Menu(self)

        self._file_menu = tk.Menu(menubar, tearoff=0)
        self._file_menu.add_command(label="Open Playlist…", command=self._open_playlist)
        self._file_menu.add_separator()
        self._add_folder_menu_items()

        menubar.add_cascade(label="File", menu=self._file_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        self._favorites_only_var = tk.BooleanVar(value=False)
        self._hide_favorites_var = tk.BooleanVar(value=False)
        view_menu.add_checkbutton(label="Favorites Only", variable=self._favorites_only_var,
                                  command=self._toggle_favorites_only)
        view_menu.add_checkbutton(label="Hide Favorites", variable=self._hide_favorites_var,
                                  command=self._toggle_hide_favorites)
        view_menu.add_separator()
        view_menu.add_command(label="Queue", command=self._open_queue_window)
        view_menu.add_command(label="Playlist Art", command=self._open_playlist_art_window)
        view_menu.add_command(label="Visualizer", command=self._open_visualizer_window)
        menubar.add_cascade(label="View", menu=view_menu)

        options_menu = tk.Menu(menubar, tearoff=0)
        self._keep_player_visible_var = tk.BooleanVar(value=True)
        self._loop_queue_var = tk.BooleanVar(value=False)
        self._shuffle_queue_var = tk.BooleanVar(value=False)
        self._loop_var = tk.BooleanVar(value=False)
        options_menu.add_checkbutton(label="Show Media Player",
                                     variable=self._keep_player_visible_var,
                                     command=self._toggle_keep_player_visible)
        options_menu.add_checkbutton(label="Repeat Queue",
                                     variable=self._loop_queue_var,
                                     command=self._toggle_loop_queue)
        options_menu.add_checkbutton(label="Shuffle",
                                     variable=self._shuffle_queue_var,
                                     command=self._toggle_shuffle_queue)
        options_menu.add_checkbutton(label="Loop",
                                     variable=self._loop_var,
                                     command=self._toggle_loop)
        menubar.add_cascade(label="Options", menu=options_menu)

        self.config(menu=menubar)

    def _build_ui(self):
        self._build_menubar()

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

        self.search_icon_label = tk.Label(search_frame, text="🔍", bg=BG_COLOR, fg=SUBTEXT_COLOR,
                 font=("Segoe UI", 11))
        self.search_icon_label.pack(side="left")

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
        search_entry.bind("<Control-a>", lambda e: (search_entry.select_range(0, "end"), "break")[1])
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
        # The 1px pady gaps between rows expose list_frame itself; without its
        # own wheel binding those gaps are scroll dead zones that make fast
        # scrolling stall when the cursor lands on one.
        self.list_frame.bind("<MouseWheel>", self._on_mousewheel)
        self.bind("<F5>", self._refresh)
        self.bind("<space>", self._on_space)
        self.bind("<Escape>", self._deselect_all)
        self.bind("<Control-a>", self._select_all)
        self.bind("<Control-o>", lambda _: self._open_playlist())

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
        self._shuffle_icon_label = tk.Label(
            player_top, text="",
            font=("Segoe UI", 10), width=2,
            bg="#0d0d1a", fg=ACCENT_COLOR,
        )
        self._shuffle_icon_label.pack(side="right", padx=(0, 4))
        self._loop_icon_label = tk.Label(
            player_top, text="",
            font=("Segoe UI", 10), width=2,
            bg="#0d0d1a", fg=ACCENT_COLOR,
        )
        self._loop_icon_label.pack(side="right", padx=(0, 2))

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
        self._player_progress.pack(fill="x", padx=10, pady=(0, 2))

        # Volume slider row
        volume_row = tk.Frame(self._player_bar_frame, bg="#0d0d1a")
        volume_row.pack(fill="x", padx=(10, 0), pady=(0, 5))

        self._vol_icon_label = tk.Label(
            volume_row, text="🔊",
            bg="#0d0d1a", fg="white",
            font=("Segoe UI", 11),
            cursor="hand2",
        )
        self._vol_icon_label.pack(side="left")
        self._vol_icon_label.bind("<Button-1>", lambda _: self._toggle_mute())
        self._vol_muted: bool = False
        self._vol_pre_mute: int = 75
        self._vol_drag_start: int = 75

        self._volume_var = tk.IntVar(value=75)
        self._vol_canvas = tk.Canvas(
            volume_row,
            width=160,
            height=20,
            bg="#0d0d1a",
            highlightthickness=0,
            cursor="hand2",
        )
        self._vol_canvas.pack(side="left", padx=(6, 6))
        self._vol_canvas.bind("<Button-1>", self._vol_canvas_press)
        self._vol_canvas.bind("<B1-Motion>", self._vol_canvas_set)
        self._vol_canvas.bind("<Configure>", lambda _: self._draw_vol_canvas())
        self._vol_canvas.bind("<MouseWheel>", lambda e: "break")

        self._player_next_btn = tk.Button(
            volume_row, text="⏭",
            font=("Segoe UI", 14),
            bg="#0d0d1a", fg=TEXT_COLOR,
            activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR,
            relief="flat", bd=0, padx=2, pady=0,
            cursor="hand2",
            command=self._queue_next,
        )
        self._player_next_btn.pack(side="right")

        self._player_play_btn = tk.Button(
            volume_row, text="▶",
            font=("Segoe UI", 14),
            bg="#0d0d1a", fg=TEXT_COLOR,
            activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR,
            relief="flat", bd=0, padx=3, pady=0,
            cursor="hand2",
            command=self._on_player_play_btn_click,
        )
        self._player_play_btn.pack(side="right", padx=(4, 4))

        self._player_back_btn = tk.Button(
            volume_row, text="⏮",
            font=("Segoe UI", 14),
            bg="#0d0d1a", fg=TEXT_COLOR,
            activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR,
            relief="flat", bd=0, padx=2, pady=0,
            cursor="hand2",
            command=self._queue_prev,
        )
        self._player_back_btn.pack(side="right", padx=(6, 0))

        self._volume_label = tk.Label(
            volume_row, text="75%", width=4,
            bg="#0d0d1a", fg=SUBTEXT_COLOR,
            font=("Segoe UI", 8),
            anchor="w",
        )
        self._volume_label.pack(side="left")

        for _w in (
            self._player_bar_frame,
            self._player_name_label,
            self._player_time_label,
            self._loop_icon_label,
            self._shuffle_icon_label,
            self._player_progress,
            volume_row,
            self._volume_label,
            self._player_back_btn,
            self._player_play_btn,
            self._player_next_btn,
        ):
            _w.bind("<Button-3>", self._show_player_context_menu)

        self._player_tick_id: str | None = None
        self._idle_anim_id: str | None = None
        self._idle_anim_frame: int = 0

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

    # ── Volume canvas slider ──────────────────────────────────────────────────

    def _draw_vol_canvas(self) -> None:
        c = self._vol_canvas
        c.delete("all")
        w = c.winfo_width()
        if w < 2:
            return
        pad = 6
        cy = c.winfo_height() // 2
        level = self._volume_var.get()
        ball_x = pad + level / 100 * (w - 2 * pad)
        c.create_line(pad, cy, w - pad, cy, fill="#2a2a3a", width=2, capstyle="round")
        if level > 0:
            c.create_line(pad, cy, ball_x, cy, fill=ACCENT_COLOR, width=2, capstyle="round")
        c.create_oval(ball_x - 5, cy - 5, ball_x + 5, cy + 5, fill="white", outline=ACCENT_COLOR, width=2)

    def _vol_canvas_press(self, event: tk.Event) -> None:
        self._vol_drag_start = self._volume_var.get()
        self._vol_canvas_set(event)

    def _vol_canvas_set(self, event: tk.Event) -> None:
        c = self._vol_canvas
        pad = 6
        track_w = max(1, c.winfo_width() - 2 * pad)
        level = max(0, min(100, int(round((event.x - pad) / track_w * 100))))
        self._volume_var.set(level)
        if level == 0 and not self._vol_muted:
            if self._vol_drag_start > 0:
                self._vol_pre_mute = self._vol_drag_start
            self._vol_muted = True
            self._vol_icon_label.config(text="🔇")
        elif level > 0 and self._vol_muted:
            self._vol_muted = False
            self._vol_icon_label.config(text="🔊")
        self._draw_vol_canvas()
        self._on_volume_change(level)

    # ── Thumbnails / list rendering ───────────────────────────────────────────

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

    def _cached_thumbnail(self, song: SongInfo) -> ImageTk.PhotoImage | None:
        """Return the cached PhotoImage for song, or None on a cache miss."""
        key = str(song.folder)
        if key in self._thumbnails:
            self._thumbnails.move_to_end(key)
            return self._thumbnails[key]
        return None

    def _store_thumbnail(self, key: str, photo: ImageTk.PhotoImage):
        self._thumbnails[key] = photo
        if len(self._thumbnails) > THUMBNAIL_CACHE_LIMIT:
            self._thumbnails.popitem(last=False)

    def _load_thumbnail(self, song: SongInfo, thumb_lbl: tk.Label | None = None) -> ImageTk.PhotoImage:
        """Return a thumbnail for song, decoding synchronously if no widget is
        given to swap into later, or a placeholder plus a background decode
        (swapped into ``thumb_lbl`` when ready) otherwise."""
        cached = self._cached_thumbnail(song)
        if cached is not None:
            return cached
        if not song.cover_path:
            return self._make_placeholder()
        if thumb_lbl is None:
            try:
                img = Image.open(song.cover_path).convert("RGB")
                img = img.resize(THUMBNAIL_SIZE, Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                self._store_thumbnail(str(song.folder), photo)
                return photo
            except Exception:
                return self._make_placeholder()
        self._load_thumbnail_async(song, thumb_lbl)
        return self._make_placeholder()

    def _load_thumbnail_async(self, song: SongInfo, thumb_lbl: tk.Label):
        key = str(song.folder)
        cover_path = song.cover_path
        gen = self._render_gen

        def worker():
            try:
                img = Image.open(cover_path).convert("RGB")
                img = img.resize(THUMBNAIL_SIZE, Image.LANCZOS)
            except Exception:
                return
            self._dispatcher.dispatch(lambda: self._on_thumbnail_decoded(gen, key, img, thumb_lbl))

        self._thumb_executor.submit(worker)

    def _on_thumbnail_decoded(self, gen: int, key: str, img: Image.Image, thumb_lbl: tk.Label):
        if gen != self._render_gen or not thumb_lbl.winfo_exists():
            return  # page/list re-rendered since this decode started; drop it
        photo = ImageTk.PhotoImage(img)
        self._store_thumbnail(key, photo)
        thumb_lbl.configure(image=photo)
        thumb_lbl.image = photo   # keep ref

    def _render_list(self):
        # Re-apply the canvas window width in case it hasn't been set yet or
        # the window was resized while a dialog was open.  This is especially
        # important under Tk 9 (Python 3.14+), which infers a label's wrap
        # width from the geometry-manager-allocated pixel width; keeping the
        # canvas window explicitly sized prevents text from wrapping after a
        # song-info edit.
        cw = self.canvas.winfo_width()
        if cw > 1:
            self.canvas.itemconfig(self.canvas_window, width=cw)

        self._render_gen += 1
        self._hide_mod_tooltip()
        self._hover_row = None   # about to destroy every row widget
        for w in self.list_frame.winfo_children():
            w.destroy()
        self._row_frames.clear()

        if self._pending_install_id:
            self._build_install_row(self._pending_install_id)
        elif self._pending_playlist_url:
            self._build_install_playlist_row(self._pending_playlist_url)

        page_start = self.page * self.page_size
        for local_idx, song in enumerate(self.filtered[page_start:page_start + self.page_size]):
            self._build_row(page_start + local_idx, song)

        for global_i in self.selected_indices:
            local_i = global_i - page_start
            if 0 <= local_i < len(self._row_frames):
                self._recolor_row(self._row_frames[local_i], SELECTED_BG)

        self._update_pagination_controls()
        self._update_scroll()

    def _build_install_playlist_row(self, url: str):
        row = tk.Frame(self.list_frame, bg=HOVER_BG, cursor="hand2")
        row.pack(fill="x", pady=1)

        icon_lbl = tk.Label(row, text="⬇", font=("Segoe UI", 20),
                            bg=HOVER_BG, fg=ACCENT_COLOR, width=4)
        icon_lbl.pack(side="left", padx=8, pady=6)

        text_frame = tk.Frame(row, bg=HOVER_BG)
        text_frame.pack(side="left", fill="both", expand=True, padx=4, pady=6)

        title_lbl = tk.Label(
            text_frame,
            text="Click to install playlist…",
            font=("Segoe UI", 11, "bold"),
            bg=HOVER_BG, fg=ACCENT_COLOR,
            anchor="w", cursor="hand2",
        )
        title_lbl.pack(fill="x")

        sub_lbl = tk.Label(
            text_frame,
            text="Downloads the playlist and its songs from BeatSaver  •  or press Enter",
            font=("Segoe UI", 9),
            bg=HOVER_BG, fg=SUBTEXT_COLOR,
            anchor="w",
        )
        sub_lbl.pack(fill="x")

        sep = tk.Frame(self.list_frame, bg=SEPARATOR_COLOR, height=1)
        sep.pack(fill="x")
        sep.bind("<MouseWheel>", self._on_mousewheel)

        for w in [row, icon_lbl, text_frame, title_lbl, sub_lbl]:
            w.bind("<Button-1>",   lambda _, u=url: self._install_playlist_from_url(u))
            w.bind("<Enter>",      lambda _, r=row: self._recolor_row(r, SELECTED_BG))
            w.bind("<Leave>",      lambda _, r=row: self._recolor_row(r, HOVER_BG))
            w.bind("<MouseWheel>", self._on_mousewheel)

    def _build_row(self, idx: int, song: SongInfo):
        # Row container
        row = tk.Frame(self.list_frame, bg=ITEM_BG, cursor="hand2")
        row.pack(fill="x", pady=1)
        self._row_frames.append(row)

        # Thumbnail: cache hit shows immediately; a miss shows the placeholder
        # and decodes off-thread, swapping the real image in when ready.
        thumb_lbl = tk.Label(row, bg=ITEM_BG, cursor="hand2")
        thumb_img = self._load_thumbnail(song, thumb_lbl)
        thumb_lbl.configure(image=thumb_img)
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
            wraplength=0,
        )
        title_lbl.pack(fill="x")

        if song.author_line:
            author_lbl = tk.Label(
                text_frame,
                text=song.author_line,
                font=("Segoe UI", 9),
                bg=ITEM_BG, fg=SUBTEXT_COLOR,
                anchor="w",
                wraplength=0,
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
                wraplength=0,
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
                    wraplength=0,
                )
                plays_lbl.pack(fill="x")
        else:
            tk.Label(
                text_frame,
                text="Easy:0 | DNF",
                font=("Courier New", 8),
                bg=ITEM_BG, fg="#555555",
                anchor="w",
                wraplength=0,
            ).pack(fill="x")
            tk.Label(
                text_frame,
                text=("★ Plays: 0") if is_fav else "Plays: 0",
                font=("Segoe UI", 8),
                bg=ITEM_BG, fg="#FFD700" if is_fav else "#555555",
                anchor="w",
                wraplength=0,
            ).pack(fill="x")

        # Separator (needs its own wheel binding — it's a scroll dead zone
        # otherwise, since Tk doesn't propagate wheel events to parents)
        sep = tk.Frame(self.list_frame, bg=SEPARATOR_COLOR, height=1)
        sep.pack(fill="x")
        sep.bind("<MouseWheel>", self._on_mousewheel)

        # Bind click / hover to all widgets in the row
        widgets = [row, thumb_lbl, text_frame, title_lbl]
        for child in text_frame.winfo_children():
            widgets.append(child)
            for grandchild in child.winfo_children():
                widgets.append(grandchild)

        browser_targets = {thumb_lbl, title_lbl}
        for w in widgets:
            w.bind("<Button-1>",        lambda e, i=idx: self._select(i, shift_held=bool(e.state & 0x1)))
            w.bind("<Button-3>",        lambda e, i=idx, s=song: self._on_right_click(e, i, s))
            w.bind("<Enter>",           lambda e, r=row, s=sep: self._hover(r, s, True))
            w.bind("<Leave>",           lambda e, r=row, s=sep: self._hover(r, s, False))
            w.bind("<MouseWheel>",      lambda e: (self._hide_mod_tooltip(), self._on_mousewheel(e)))
            if w in browser_targets:
                w.bind("<Control-Button-1>",
                       lambda _, s=song: webbrowser.open(f"https://beatsaver.com/maps/{s.song_id}") if s.song_id else None)
            else:
                w.bind("<Control-Button-1>", lambda e, i=idx: self._select(i, ctrl_held=True))

        # Mod tooltip only triggers off the song art thumbnail, not the whole row,
        # and follows the cursor while it stays over the art. Stash the song on the
        # widget so _on_scroll_idle can re-show the tip for whatever art the cursor
        # settled on when a scroll burst suppressed its Enter.
        thumb_lbl._mod_song = song
        thumb_lbl.bind("<Enter>", lambda e, sg=song: self._show_mod_tooltip(e, sg), add="+")
        thumb_lbl.bind("<Motion>", lambda e, sg=song: self._on_mod_tooltip_motion(e, sg), add="+")
        thumb_lbl.bind("<Leave>", lambda e: self._hide_mod_tooltip(), add="+")

    # ── Hover / selection / row coloring ──────────────────────────────────────

    def _hover(self, row: tk.Frame, sep: tk.Frame, entering: bool):
        if self._scroll_active:
            return  # suppressed during wheel-scroll bursts; see _on_mousewheel
        self._hover_row = row if entering else None
        if self._row_is_selected(row):
            return
        bg = HOVER_BG if entering else ITEM_BG
        self._recolor_row(row, bg)

    def _show_mod_tooltip(self, event: tk.Event, song: SongInfo):
        """Show a small tooltip listing required/suggested mods for a row.

        No-ops when the song needs no mods at all.
        """
        self._hide_mod_tooltip()
        if self._scroll_active and not self._pointer_moved(event):
            # A stationary cursor with rows sweeping under it fires an Enter per
            # thumbnail; spawning a Toplevel for each hitches high-speed scrolling,
            # so skip those. But if the pointer actually moved, the user is
            # genuinely mousing over the art — show the tip even mid-scroll.
            return
        if not (song.mod_required or song.mod_suggested):
            return

        lines = []
        if song.mod_required:
            lines.append("Required: " + ", ".join(sorted(song.mod_required)))
        if song.mod_suggested:
            lines.append("Suggested: " + ", ".join(sorted(song.mod_suggested)))

        tip = tk.Toplevel(self)
        tip.wm_overrideredirect(True)
        try:
            tip.attributes("-topmost", True)
        except tk.TclError:
            pass
        tip.wm_geometry(f"+{event.x_root + 14}+{event.y_root + 12}")

        tk.Label(
            tip, text="\n".join(lines), justify="left",
            bg="#1e1e1e", fg=TEXT_COLOR, font=("Segoe UI", 9),
            relief="solid", bd=1, padx=6, pady=4,
        ).pack()

        self._mod_tooltip = tip

    def _on_mod_tooltip_motion(self, event: tk.Event, song: SongInfo):
        """Handle real pointer motion over a song's art.

        Motion only fires from genuine cursor movement (rows sweeping under a
        stationary cursor produce Enter/Leave, not Motion), so this is the
        reliable "user is actively mousing over the art" signal. Reposition an
        existing tip, or create one that a scroll burst suppressed at Enter time.
        """
        if self._mod_tooltip is not None:
            self._move_mod_tooltip(event)
        else:
            self._show_mod_tooltip(event, song)

    def _move_mod_tooltip(self, event: tk.Event):
        """Reposition an already-shown tooltip to follow the cursor.

        No-ops if there's no active tooltip (song needs no mods).
        """
        tip = self._mod_tooltip
        if tip is None:
            return
        try:
            tip.wm_geometry(f"+{event.x_root + 14}+{event.y_root + 12}")
        except tk.TclError:
            pass

    def _pointer_moved(self, event: tk.Event, threshold: int = 2) -> bool:
        """True if the pointer moved since the last event we recorded its position.

        Used to tell genuine hover (cursor displaced) from the Enter/Leave storm
        of rows scrolling under a stationary cursor (cursor fixed). Records the
        current position as the new baseline so the next comparison is relative.
        """
        xy = (event.x_root, event.y_root)
        last = self._last_ptr_xy
        self._last_ptr_xy = xy
        if last is None:
            return True
        return abs(xy[0] - last[0]) > threshold or abs(xy[1] - last[1]) > threshold

    def _show_mod_tooltip_under_pointer(self):
        """Show the mod tooltip for whatever song art the cursor is resting on.

        Called when a scroll burst ends: the cursor may have come to rest on art
        whose Enter was suppressed mid-scroll, with no further event coming while
        it sits still, so nothing would otherwise surface the tip.
        """
        try:
            x = self.winfo_pointerx()
            y = self.winfo_pointery()
            widget = self.winfo_containing(x, y)
        except (tk.TclError, KeyError):
            return
        song = getattr(widget, "_mod_song", None)
        if song is None:
            return
        event = tk.Event()
        event.x_root = x
        event.y_root = y
        self._show_mod_tooltip(event, song)

    def _hide_mod_tooltip(self, *_):
        tip = self._mod_tooltip
        if tip is not None:
            try:
                tip.destroy()
            except tk.TclError:
                pass
            self._mod_tooltip = None

    def _row_is_selected(self, row: tk.Frame) -> bool:
        return row.cget("bg") == SELECTED_BG

    def _recolor_row(self, row: tk.Frame, bg: str):
        # Rows are static after they're built, so collect each row's widget
        # tree once (depth-agnostic, like the old recursion) and reuse the
        # flat list — recursing winfo_children() on every hover/selection
        # recolor cost dozens of Tcl round-trips per event, which added up
        # badly during scroll-induced Enter/Leave storms.
        widgets = getattr(row, "_recolor_widgets", None)
        if widgets is None:
            widgets = []
            stack = [row]
            while stack:
                w = stack.pop()
                widgets.append(w)
                stack.extend(w.winfo_children())
            row._recolor_widgets = widgets
        for w in widgets:
            try:
                w.configure(bg=bg)
            except Exception:
                pass

    def _deselect_all(self, _event=None):
        page_start = self.page * self.page_size
        for i in self.selected_indices:
            li = i - page_start
            if 0 <= li < len(self._row_frames):
                self._recolor_row(self._row_frames[li], ITEM_BG)
        self.selected_indices.clear()
        self._selected_folders.clear()
        self.selected_index = None
        self.status_bar.config(text="")

    def _select_all(self, _event=None):
        self.selected_indices = set(range(len(self.filtered)))
        self._selected_folders = {str(s.folder) for s in self.filtered}
        self.selected_index = len(self.filtered) - 1 if self.filtered else None
        for row in self._row_frames:
            self._recolor_row(row, SELECTED_BG)
        n = len(self.filtered)
        self.status_bar.config(text=f"{n} song{'s' if n != 1 else ''} selected")
        return "break"

    def _select(self, idx: int, shift_held: bool = False, ctrl_held: bool = False):
        self.canvas.focus_set()
        page_start = self.page * self.page_size
        local_idx = idx - page_start

        if ctrl_held:
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
            if len(self._selected_folders) > 1:
                self.status_bar.config(text=f"{len(self._selected_folders)} songs selected")
            elif self.selected_index is not None:
                song = self.filtered[self.selected_index]
                self.status_bar.config(
                    text=f"Selected: {song.display_name}"
                         + (f"  •  {song.author}" if song.author else "")
                         + (f"  •  {song.bpm_str}" if song.bpm_str else "")
                )
            else:
                self.status_bar.config(text="")
            return

        if shift_held and len(self.selected_indices) == 1:
            anchor = next(iter(self.selected_indices))
            lo, hi = min(anchor, idx), max(anchor, idx)
            for i in self.selected_indices:
                li = i - page_start
                if 0 <= li < len(self._row_frames):
                    self._recolor_row(self._row_frames[li], ITEM_BG)
            self.selected_indices = set(range(lo, hi + 1))
            self._selected_folders = {
                str(self.filtered[i].folder)
                for i in self.selected_indices
                if i < len(self.filtered)
            }
            self.selected_index = idx
            for i in self.selected_indices:
                li = i - page_start
                if 0 <= li < len(self._row_frames):
                    self._recolor_row(self._row_frames[li], SELECTED_BG)
        elif shift_held:
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
        elif len(self.selected_indices) == 1 and idx in self.selected_indices:
            self.selected_indices.clear()
            self._selected_folders.clear()
            self.selected_index = None
            if 0 <= local_idx < len(self._row_frames):
                self._recolor_row(self._row_frames[local_idx], ITEM_BG)
            self.status_bar.config(text="")
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
