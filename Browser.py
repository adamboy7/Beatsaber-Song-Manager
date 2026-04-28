"""
Beat Saber Custom Song Browser
Parses Steam library to locate Beat Saber, then lists all custom songs
with cover art and metadata. Click art or title to select a song.
"""

import os
import re
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


class SongBrowser(tk.Tk):
    def __init__(self, custom_levels: Path):
        super().__init__()
        self.custom_levels = custom_levels
        self.songs: list[SongInfo] = []
        self.filtered: list[SongInfo] = []
        self.selected_index: int | None = None
        self.selected_indices: set[int] = set()
        self._thumbnails: dict[int, ImageTk.PhotoImage] = {}   # keep refs alive
        self._placeholder: ImageTk.PhotoImage | None = None
        self._row_frames: list[tk.Frame] = []
        self._pending_install_id: str | None = None

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
        self._media_player.start_media_keys(self.after)

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

    def _load_thumbnail(self, song: SongInfo, idx: int) -> ImageTk.PhotoImage:
        if idx in self._thumbnails:
            return self._thumbnails[idx]
        try:
            if song.cover_path:
                img = Image.open(song.cover_path).convert("RGB")
                img = img.resize(THUMBNAIL_SIZE, Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                self._thumbnails[idx] = photo
                return photo
        except Exception:
            pass
        return self._make_placeholder()

    def _render_list(self):
        # Clear existing rows
        for w in self.list_frame.winfo_children():
            w.destroy()
        self._row_frames.clear()

        if self._pending_install_id:
            self._build_install_row(self._pending_install_id)

        for idx, song in enumerate(self.filtered):
            self._build_row(idx, song)

        for i in self.selected_indices:
            if i < len(self._row_frames):
                self._recolor_row(self._row_frames[i], SELECTED_BG)

        self._update_scroll()

    def _is_favorite(self, song: SongInfo) -> bool:
        return any(lid in self.favorite_ids for lid in song_level_ids(song))

    def _build_row(self, idx: int, song: SongInfo):
        # Row container
        row = tk.Frame(self.list_frame, bg=ITEM_BG, cursor="hand2")
        row.pack(fill="x", pady=1)
        self._row_frames.append(row)

        # Thumbnail (loaded lazily)
        thumb_img = self._load_thumbnail(song, self.filtered.index(song))
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

    # ── Song operations ───────────────────────────────────────────────────────

    def _play_audio(self, song: SongInfo):
        self._media_player.play(song)

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
        menu.add_command(label="Play Audio",
                         command=lambda: self._play_audio(song),
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

    def _on_right_click(self, event: tk.Event, idx: int, song: SongInfo):
        if len(self.selected_indices) > 1 and idx in self.selected_indices:
            songs = [self.filtered[i] for i in sorted(self.selected_indices)]
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
        menu.add_command(label="Play", state="disabled")
        menu.add_separator()
        menu.add_command(label="Add to Favorites",
                         command=lambda: self._add_to_favorites_multi(songs),
                         state=fav_state)
        menu.add_command(label="Remove from Favorites",
                         command=lambda: self._remove_from_favorites_multi(songs),
                         state=fav_state)
        menu.add_separator()
        menu.add_command(label="Share Playlist", state="disabled")
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
        self.selected_index = None
        self.selected_indices = set()
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
        self.songs    = [s for s in self.songs    if id(s) not in deleted_ids]
        self.filtered = [s for s in self.filtered if id(s) not in deleted_ids]
        self.selected_index = None
        self.selected_indices = set()
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

        if shift_held:
            if idx in self.selected_indices:
                self.selected_indices.discard(idx)
                if idx < len(self._row_frames):
                    self._recolor_row(self._row_frames[idx], ITEM_BG)
                if self.selected_index == idx:
                    self.selected_index = max(self.selected_indices) if self.selected_indices else None
            else:
                self.selected_indices.add(idx)
                self.selected_index = idx
                if idx < len(self._row_frames):
                    self._recolor_row(self._row_frames[idx], SELECTED_BG)
        else:
            for i in self.selected_indices:
                if i < len(self._row_frames):
                    self._recolor_row(self._row_frames[i], ITEM_BG)
            self.selected_indices = {idx}
            self.selected_index = idx
            self._recolor_row(self._row_frames[idx], SELECTED_BG)

        if len(self.selected_indices) > 1:
            self.status_bar.config(text=f"{len(self.selected_indices)} songs selected")
        elif self.selected_index is not None:
            song = self.filtered[self.selected_index]
            self.status_bar.config(
                text=f"Selected: {song.display_name}"
                     + (f"  •  {song.author}" if song.author else "")
                     + (f"  •  {song.bpm_str}" if song.bpm_str else "")
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
            self.selected_index = None
            self.selected_indices = set()
            self._thumbnails.clear()
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
        self.selected_index = None
        self.selected_indices = set()
        self._thumbnails.clear()   # free memory; will reload on render
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
