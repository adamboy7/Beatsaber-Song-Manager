"""
Beat Saber Custom Song Browser
Parses Steam library to locate Beat Saber, then lists all custom songs
with cover art and metadata. Click art or title to select a song.
"""

import os
import re
import json
import subprocess
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from PIL import Image, ImageTk
import time
import datetime
import threading

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
from libraries.audio_utils import find_ffmpeg, find_ffplay
from libraries.asset_editor import bak_files, restore_files, replace_art, replace_audio


class SongBrowser(tk.Tk):
    def __init__(self, custom_levels: Path):
        super().__init__()
        self.custom_levels = custom_levels
        self.songs: list[SongInfo] = []
        self.filtered: list[SongInfo] = []
        self.selected_index: int | None = None
        self._thumbnails: dict[int, ImageTk.PhotoImage] = {}   # keep refs alive
        self._placeholder: ImageTk.PhotoImage | None = None
        self._row_frames: list[tk.Frame] = []
        self._audio_proc: subprocess.Popen | None = None
        self._audio_paused: bool = False
        self._playing_song: SongInfo | None = None

        self.player_stats: dict = {}
        self.favorite_ids: set[str] = set()
        self.player_dat_path: Path | None = None
        self._pending_install_id: str | None = None
        self._install_gen: int = 0
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

        self._kb_listener = None
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_ui()
        self._start_media_keys()
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
            w.bind("<Button-1>",         lambda e, i=idx: self._select(i))
            w.bind("<Control-Button-1>",   lambda _, s=song: webbrowser.open(f"https://beatsaver.com/maps/{s.song_id}") if s.song_id else None)
            w.bind("<Button-3>",         lambda e, s=song: self._show_context_menu(e, s))
            w.bind("<Enter>",       lambda e, r=row, s=sep: self._hover(r, s, True))
            w.bind("<Leave>",       lambda e, r=row, s=sep: self._hover(r, s, False))
            w.bind("<MouseWheel>",  self._on_mousewheel)

    def _favorite_level_id(self, song: SongInfo) -> str:
        if song.song_hash:
            return f"custom_level_{song.song_hash}"
        return f"custom_level_{song.folder.name}"

    def _add_to_favorites(self, song: SongInfo):
        if not self.player_dat_path:
            return
        try:
            raw = self.player_dat_path.read_text(encoding="utf-8", errors="replace")
            self._backup_player_data(raw)
            data = json.loads(raw)
            players = data.get("localPlayers", [])
            if not players:
                return
            level_id = self._favorite_level_id(song)
            favs: list = players[0].setdefault("favoritesLevelIds", [])
            if level_id not in favs:
                favs.append(level_id)
            self.player_dat_path.write_text(
                json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            self.favorite_ids.add(level_id)
            self._render_list()
        except Exception as exc:
            messagebox.showerror("Favorites Error", str(exc))

    def _backup_player_data(self, raw: str):
        """Write a timestamped backup to backups/ and a plain .bak alongside PlayerData."""
        bak_dir = Path(__file__).parent / "backups"
        bak_dir.mkdir(exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        (bak_dir / f"PlayerData_{stamp}.dat.bak").write_text(raw, encoding="utf-8")
        self.player_dat_path.with_suffix(".dat.bak").write_text(raw, encoding="utf-8")

    def _remove_from_favorites(self, song: SongInfo):
        if not self.player_dat_path:
            return
        try:
            raw = self.player_dat_path.read_text(encoding="utf-8", errors="replace")
            self._backup_player_data(raw)
            data = json.loads(raw)
            players = data.get("localPlayers", [])
            if not players:
                return
            # Scrub every known levelId form for this song
            to_remove = {f"custom_level_{song.folder.name}"}
            if song.song_hash:
                to_remove.add(f"custom_level_{song.song_hash}")
            favs: list = players[0].get("favoritesLevelIds", [])
            players[0]["favoritesLevelIds"] = [f for f in favs if f not in to_remove]
            self.player_dat_path.write_text(
                json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            self.favorite_ids -= to_remove
            self._render_list()
        except Exception as exc:
            messagebox.showerror("Favorites Error", str(exc))

    def _on_close(self):
        self._install_gen += 1  # cancel any pending install watch
        if self._kb_listener:
            self._kb_listener.stop()
        self._stop_audio()
        self.destroy()

    def _start_media_keys(self):
        from pynput import keyboard as pynput_kb

        def on_press(key):
            if key == pynput_kb.Key.media_play_pause:
                self.after(0, self._toggle_pause_audio)
            elif key == pynput_kb.Key.media_stop:
                self.after(0, self._stop_audio)

        self._kb_listener = pynput_kb.Listener(on_press=on_press)
        self._kb_listener.daemon = True
        self._kb_listener.start()

    def _on_space(self, *_):
        if self.focus_get() is self.search_entry:
            return
        self._toggle_pause_audio()

    def _toggle_pause_audio(self):
        if not self._audio_proc or self._audio_proc.poll() is not None:
            self._audio_paused = False
            return
        try:
            import ctypes
            ntdll   = ctypes.WinDLL("ntdll")
            kernel32 = ctypes.WinDLL("kernel32")
            handle = kernel32.OpenProcess(0x1F0FFF, False, self._audio_proc.pid)
            if self._audio_paused:
                ntdll.NtResumeProcess(handle)
                self._audio_paused = False
            else:
                ntdll.NtSuspendProcess(handle)
                self._audio_paused = True
            kernel32.CloseHandle(handle)
        except Exception as exc:
            messagebox.showerror("Pause Failed", str(exc))

    def _stop_audio(self):
        if self._audio_proc and self._audio_proc.poll() is None:
            self._audio_proc.terminate()
        self._audio_proc = None
        self._playing_song = None

    def _play_audio(self, song: SongInfo):
        if not song.audio_path:
            messagebox.showwarning("Play Audio", "This song has no audio file.")
            return
        self._stop_audio()
        ffplay = find_ffplay()
        if ffplay:
            try:
                self._audio_proc = subprocess.Popen(
                    [ffplay, "-nodisp", "-autoexit", str(song.audio_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._playing_song = song
            except Exception as exc:
                messagebox.showerror("Play Audio Failed", str(exc))
        else:
            ext = song.audio_path.suffix.lower()
            if ext == ".ogg":
                try:
                    os.startfile(song.audio_path)
                except Exception as exc:
                    messagebox.showerror("Play Audio Failed", str(exc))
            else:
                messagebox.showwarning(
                    "Play Audio",
                    "ffplay not found. Place ffplay.exe next to this script or add it to your PATH.",
                )

    def _restore_files(self, song: SongInfo):
        baks = bak_files(song)
        if not baks:
            return
        errors = restore_files(song)
        if errors:
            messagebox.showerror("Restore Failed", "\n".join(errors))
        else:
            self._thumbnails.clear()
            self._render_list()
            self.status_bar.config(text=f"Restored {len(baks)} file(s) for: {song.display_name}")

    def _replace_art(self, song: SongInfo):
        if not song.cover_path:
            messagebox.showwarning("Replace Art", "This song has no cover image to replace.")
            return

        import tkinter.filedialog as fd
        new_path_str = fd.askopenfilename(
            title="Select New Cover Image",
            filetypes=[("Image files", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"), ("All files", "*.*")],
        )
        if not new_path_str:
            return

        try:
            replace_art(song.cover_path, new_path_str)
            self._thumbnails.clear()
            self._render_list()
        except Exception as exc:
            messagebox.showerror("Replace Art Failed", str(exc))

    def _prompt_ffmpeg_download(self):
        dlg = tk.Toplevel(self)
        dlg.title("ffmpeg Required")
        dlg.configure(bg=BG_COLOR)
        dlg.resizable(False, False)
        dlg.grab_set()

        tk.Label(
            dlg,
            text="ffmpeg is required to convert audio files.\n"
                 "Download it and add ffmpeg.exe to your PATH.",
            font=("Segoe UI", 10),
            bg=BG_COLOR, fg=TEXT_COLOR,
            padx=24, pady=18,
            justify="center",
        ).pack()

        btn_frame = tk.Frame(dlg, bg=BG_COLOR)
        btn_frame.pack(pady=(0, 16))

        tk.Button(
            btn_frame,
            text="Download",
            font=("Segoe UI", 10),
            bg=ACCENT_COLOR, fg=TEXT_COLOR,
            activebackground="#a01d90", activeforeground=TEXT_COLOR,
            relief="flat", padx=14, pady=5,
            command=lambda: (
                webbrowser.open("https://ffmpeg.org/download.html#build-windows"),
                dlg.destroy(),
            ),
        ).pack(side="left", padx=6)

        tk.Button(
            btn_frame,
            text="Cancel",
            font=("Segoe UI", 10),
            bg="#333333", fg=TEXT_COLOR,
            activebackground="#444444", activeforeground=TEXT_COLOR,
            relief="flat", padx=14, pady=5,
            command=dlg.destroy,
        ).pack(side="left", padx=6)

        dlg.wait_window()

    def _replace_audio(self, song: SongInfo):
        if not song.audio_path:
            messagebox.showwarning("Replace Audio", "This song has no audio file to replace.")
            return

        import tkinter.filedialog as fd
        new_path_str = fd.askopenfilename(
            title="Select New Audio File",
            filetypes=[
                ("Audio files", "*.mp3 *.wav *.ogg *.egg *.m4a"),
                ("MP3",         "*.mp3"),
                ("WAV",         "*.wav"),
                ("OGG / EGG",   "*.ogg *.egg"),
                ("M4A",         "*.m4a"),
                ("All files",   "*.*"),
            ],
        )
        if not new_path_str:
            return

        ffmpeg_path = find_ffmpeg()
        if not ffmpeg_path and Path(new_path_str).suffix.lower() not in (".egg", ".ogg"):
            self._prompt_ffmpeg_download()
            return

        try:
            replace_audio(song.audio_path, new_path_str, ffmpeg_path or "")
            self.status_bar.config(text=f"Audio replaced for: {song.display_name}")
        except Exception as exc:
            messagebox.showerror("Replace Audio Failed", str(exc))

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
        ids_to_clear = set(song_level_ids(song))
        try:
            raw = self.player_dat_path.read_text(encoding="utf-8", errors="replace")
            self._backup_player_data(raw)
            data = json.loads(raw)
            players = data.get("localPlayers", [])
            if not players:
                return
            entries = players[0].get("levelsStatsData", [])
            before = len(entries)
            players[0]["levelsStatsData"] = [
                e for e in entries if e.get("levelId", "") not in ids_to_clear
            ]
            removed = before - len(players[0]["levelsStatsData"])
            self.player_dat_path.write_text(
                json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            self.player_stats = load_player_stats(self.player_dat_path)
            self._render_list()
            self.status_bar.config(
                text=f"Cleared {removed} score entr{'y' if removed == 1 else 'ies'} for: {song.display_name}"
            )
        except Exception as exc:
            messagebox.showerror("Clear Score Failed", str(exc))

    def _delete_song(self, song: SongInfo):
        msg = f'Delete "{song.display_name}"?\n\nThe folder will be removed from CustomLevels. Your scores will not be affected.'
        if not messagebox.askyesno("Delete Song", msg, icon="warning", default="no"):
            return
        if song is self._playing_song:
            proc = self._audio_proc
            self._stop_audio()
            if proc:
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        try:
            import shutil
            shutil.rmtree(song.folder)
        except Exception as exc:
            messagebox.showerror("Delete Failed", str(exc))
            return
        self.songs    = [s for s in self.songs    if s is not song]
        self.filtered = [s for s in self.filtered if s is not song]
        self.selected_index = None
        self._thumbnails.clear()
        self._render_list()
        self.count_label.config(text=f"({len(self.songs)} songs)")
        self.status_bar.config(text=f"{len(self.filtered)} songs shown")

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

    def _select(self, idx: int):
        self.canvas.focus_set()
        # Deselect previous
        if self.selected_index is not None and self.selected_index < len(self._row_frames):
            self._recolor_row(self._row_frames[self.selected_index], ITEM_BG)

        self.selected_index = idx
        self._recolor_row(self._row_frames[idx], SELECTED_BG)

        song = self.filtered[idx]
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
            self._thumbnails.clear()
            self._render_list()
            return

        self._pending_install_id = None
        self._install_gen += 1  # cancel any pending install watch
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
        self._thumbnails.clear()   # free memory; will reload on render
        self._render_list()
        self.status_bar.config(text=f"{len(self.filtered)} songs shown")

    def _on_search_enter(self, *_):
        if self._pending_install_id:
            self._trigger_install(self._pending_install_id)

    def _trigger_install(self, song_id: str):
        if not self._has_beatsaver_handler():
            self.status_bar.config(
                text="No handler for beatsaver:// — install Mod Assistant and enable one-click installs."
            )
            return
        webbrowser.open(f"beatsaver://{song_id}")
        self._watch_for_install(song_id)

    @staticmethod
    def _has_beatsaver_handler() -> bool:
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "beatsaver")
            winreg.CloseKey(key)
            return True
        except (FileNotFoundError, OSError):
            return False

    def _watch_for_install(self, song_id: str):
        self._install_gen += 1
        gen = self._install_gen
        self._pulse_install_status(song_id, gen, 0)

        def worker():
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if gen != self._install_gen:
                    return
                try:
                    for entry in self.custom_levels.iterdir():
                        if not entry.is_dir():
                            continue
                        name = entry.name.lower()
                        if name.startswith(song_id + " ") or name == song_id:
                            if self._is_song_complete(entry):
                                if gen == self._install_gen:
                                    self.after(0, lambda: self._on_install_complete(gen))
                                return
                except Exception:
                    pass
                time.sleep(1)
            if gen == self._install_gen:
                self.after(0, lambda: self._on_install_timeout(song_id, gen))

        threading.Thread(target=worker, daemon=True).start()

    def _pulse_install_status(self, song_id: str, gen: int, elapsed: int):
        if gen != self._install_gen:
            return
        dots = "." * (elapsed % 4)
        self.status_bar.config(text=f"Waiting for {song_id} to install{dots}  ({elapsed}s)")
        if elapsed < 30:
            self.after(1000, lambda: self._pulse_install_status(song_id, gen, elapsed + 1))

    def _is_song_complete(self, folder: Path) -> bool:
        info_file = None
        for name in ("Info.dat", "info.dat"):
            candidate = folder / name
            if candidate.exists():
                info_file = candidate
                break
        if not info_file:
            return False
        try:
            data = json.loads(info_file.read_text(encoding="utf-8", errors="replace"))
            audio = data.get("_songFilename", "")
            if audio and not (folder / audio).exists():
                return False
            cover = data.get("_coverImageFilename", "")
            if cover and not (folder / cover).exists():
                return False
            for bms in data.get("_difficultyBeatmapSets", []):
                for bm in bms.get("_difficultyBeatmaps", []):
                    diff_file = bm.get("_beatmapFilename", "")
                    if diff_file and not (folder / diff_file).exists():
                        return False
            return True
        except Exception:
            return False

    def _on_install_complete(self, gen: int):
        if gen != self._install_gen:
            return
        self._install_gen += 1  # stop the pulse

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

    def _on_install_timeout(self, song_id: str, gen: int):
        if gen != self._install_gen:
            return
        self._install_gen += 1  # stop any late-firing pulse from overwriting the message
        self.status_bar.config(
            text=f"No install detected for {song_id} — check that Mod Assistant is running and one-click installs are enabled."
        )

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
