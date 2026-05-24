"""
Beat Saber Custom Song Browser.
Parses Steam library to locate Beat Saber, then lists all custom songs
with cover art and metadata. Click art or title to select a song.

The browser's behavior is split across themed mixins in libraries/:
  • browser_ui          — menus, layout, list rendering, selection
  • browser_playback    — queue, player bar, ticks, hotkeys
  • browser_playlists   — song loading, view filters, .bplist I/O,
                          sub-window orchestration
  • browser_actions     — favorites, song operations, context menus
  • browser_pagination  — pagination, search, install, scroll helpers

Sub-windows (QueueWindow, PlaylistArtWindow) live in their own
library modules.
"""

import argparse
import base64
import io
import json
import random
import sys
import tkinter as tk
from tkinter import messagebox
from tkinterdnd2 import TkinterDnD
from pathlib import Path
from PIL import ImageTk

from libraries.constants import BG_COLOR, WINDOW_TITLE
from libraries.steam_paths import find_beatsaber_custom_levels
from libraries.song_data import SongInfo, load_songs, load_song_hashes
from libraries.player_data import (
    find_player_data, load_favorites, load_player_stats,
)
from libraries.media_player import MediaPlayer
from libraries.install_manager import InstallManager
from libraries.playlist_installer import PlaylistInstaller
from libraries.queue_window import QueueWindow
from libraries.playlist_art_window import PlaylistArtWindow
from libraries.browser_ui import BrowserUIMixin
from libraries.browser_playback import BrowserPlaybackMixin
from libraries.browser_playlists import BrowserPlaylistsMixin
from libraries.browser_actions import BrowserActionsMixin
from libraries.browser_pagination import BrowserPaginationMixin, filter_songs, pick_random_songs

PAGE_SIZE = 50


class SongBrowser(
    BrowserUIMixin,
    BrowserPlaybackMixin,
    BrowserPlaylistsMixin,
    BrowserActionsMixin,
    BrowserPaginationMixin,
    TkinterDnD.Tk,
):
    def __init__(self, custom_levels: Path, startup_playlist: Path | None = None, startup_random_groups: list[tuple[int, str | None]] | None = None):
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
        self._pending_playlist_url: str | None = None
        self._pending_playlist_temp_path: Path | None = None
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
        try:
            _icon = tk.PhotoImage(file=Path(__file__).parent / "Icon.png")
            self.iconphoto(True, _icon)
        except Exception:
            pass

        self._favorites_only: bool = False
        self._hide_favorites: bool = False
        self._keep_player_visible: bool = True
        self._loop_queue: bool = False
        self._shuffle_queue: bool = False
        self._last_shuffle_index: int | None = None

        self._build_ui()

        self._media_player = MediaPlayer()
        self._media_player.start_media_keys(self.after, self._stop_playback, self._queue_next, self._queue_prev)
        self._queue: list[SongInfo] = []
        self._queue_index: int = -1
        self._player_bar_visible: bool = False
        if self._keep_player_visible:
            self._show_player_bar_idle(None, None)
            self._player_bar_frame.pack(fill="x", padx=16, pady=(0, 4), before=self.status_bar)
            self._player_bar_visible = True
        self._queue_window: QueueWindow | None = None
        self._playlist_art_b64: str | None = None
        self._playlist_art_locked: bool = False
        self._playlist_art_first_song_key: str | None = None
        self._playlist_art_window: PlaylistArtWindow | None = None
        self._pending_playlist_entries: list[dict] | None = None
        self._pending_playlist_queue: list[dict] = []
        self._startup_playlist: Path | None = startup_playlist
        self._startup_random_groups: list[tuple[int, str | None]] = startup_random_groups or []

        self._install_manager = InstallManager(
            custom_levels,
            self.after,
            lambda text: self.status_bar.config(text=text),
            self._on_install_complete_reload,
        )
        self._playlist_installer = PlaylistInstaller(
            custom_levels,
            self.after,
            lambda text: self.status_bar.config(text=text),
            self._on_playlist_install_complete,
        )

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._setup_playlist_dnd()
        self._load_async()

    def _on_close(self):
        self._stop_idle_animation()
        self._install_manager.cancel()
        self._playlist_installer.cancel()
        self._media_player.stop_listener()
        self._media_player.stop()
        self.destroy()


# ─── Entry point ──────────────────────────────────────────────────────────────

def _load_player_data_headless():
    pd, _ = find_player_data()
    if pd:
        return load_player_stats(pd), load_favorites(pd)
    return {}, set()


def main():
    # Normalize --randomadd (any casing) to --randomAdd before parsing
    normalized = []
    for a in sys.argv:
        flag, sep, val = a.partition("=")
        if flag.lower() == "--randomadd":
            a = f"--randomAdd{sep}{val}"
        normalized.append(a)
    sys.argv = normalized

    parser = argparse.ArgumentParser(description="Beat Saber Song Manager")
    parser.add_argument("playlist", nargs="?", help="Playlist file (.bplist / .json)")
    parser.add_argument("--shuffle", action="store_true", help="Shuffle playlist order and write back to file (headless)")
    parser.add_argument("--randomAdd", nargs='+', action='append', metavar=("N", "FILTER"), help="Add N random songs (optional inline filters). May be used multiple times.")
    args = parser.parse_args()

    def _parse_random_groups(raw_groups):
        if not raw_groups:
            return []
        groups = []
        for parts in raw_groups:
            count = int(parts[0])
            filters = [p.strip(',') for p in parts[1:] if p.strip(',')]
            filter_str = ' '.join(filters) if filters else None
            groups.append((count, filter_str))
        return groups

    random_groups = _parse_random_groups(args.randomAdd)

    playlist_path: Path | None = None
    if args.playlist:
        candidate = Path(args.playlist)
        if candidate.suffix.lower() in {".bplist", ".json"} and candidate.is_file():
            playlist_path = candidate

    if args.shuffle:
        if playlist_path is None:
            print("--shuffle requires a valid .bplist or .json playlist file.")
            sys.exit(1)
        with open(playlist_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        playlist_songs = data.get("songs")
        if not isinstance(playlist_songs, list):
            print("Playlist has no 'songs' array.")
            sys.exit(1)

        if random_groups:
            custom_levels = find_beatsaber_custom_levels()
            if custom_levels is None:
                print("--randomAdd requires Beat Saber to be found automatically.")
                sys.exit(1)
            library = load_songs(custom_levels)
            hashes = load_song_hashes(custom_levels)
            for song in library:
                song.song_hash = hashes.get(song.folder.name, "")
            existing = {(e.get("hash") or "").upper() for e in playlist_songs}
            remaining_candidates = [s for s in library if s.song_hash and s.song_hash.upper() not in existing]
            if not remaining_candidates:
                print("Warning: no songs found in library; skipping randomAdd.")
            else:
                ps, fi = _load_player_data_headless()
                total_added = 0
                for count, filter_str in random_groups:
                    filtered_candidates = None
                    if filter_str:
                        filtered_candidates = filter_songs(remaining_candidates, filter_str, ps, fi)
                        if not filtered_candidates:
                            print(f"Warning: filter '{filter_str}' matched no songs; falling back to unfiltered picks.")
                    picks = pick_random_songs(filtered_candidates, remaining_candidates, count)
                    for song in picks:
                        playlist_songs.append({
                            "key": song.song_id,
                            "hash": song.song_hash,
                            "songName": song.display_name,
                        })
                    picked_folders = {s.folder for s in picks}
                    remaining_candidates = [s for s in remaining_candidates if s.folder not in picked_folders]
                    total_added += len(picks)
                print(f"Added {total_added} random song(s) to {playlist_path.name}")

        random.shuffle(playlist_songs)
        data["songs"] = playlist_songs
        with open(playlist_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"Shuffled {len(playlist_songs)} songs in {playlist_path.name}")
        sys.exit(0)

    # Headless: --randomAdd N with a playlist path that doesn't exist yet → create it and exit
    if random_groups and args.playlist:
        candidate = Path(args.playlist)
        if not candidate.is_file() and candidate.suffix.lower() in {".bplist", ".json"}:
            custom_levels = find_beatsaber_custom_levels()
            if custom_levels is None:
                print("--randomAdd requires Beat Saber to be found automatically.")
                sys.exit(1)
            library = load_songs(custom_levels)
            hashes = load_song_hashes(custom_levels)
            for song in library:
                song.song_hash = hashes.get(song.folder.name, "")
            remaining_candidates = [s for s in library if s.song_hash]
            if not remaining_candidates:
                print("Warning: no songs with hashes found in library.")
                sys.exit(1)
            ps, fi = _load_player_data_headless()
            all_picks = []
            for count, filter_str in random_groups:
                filtered_candidates = None
                if filter_str:
                    filtered_candidates = filter_songs(remaining_candidates, filter_str, ps, fi)
                    if not filtered_candidates:
                        print(f"Warning: filter '{filter_str}' matched no songs; falling back to unfiltered picks.")
                picks = pick_random_songs(filtered_candidates, remaining_candidates, count)
                all_picks.extend(picks)
                picked_folders = {s.folder for s in picks}
                remaining_candidates = [s for s in remaining_candidates if s.folder not in picked_folders]
            image_data = ""
            for song in all_picks:
                if song.cover_path and song.cover_path.exists():
                    try:
                        from PIL import Image
                        buf = io.BytesIO()
                        Image.open(song.cover_path).convert("RGB").save(buf, format="JPEG")
                        image_data = base64.b64encode(buf.getvalue()).decode("ascii")
                    except Exception:
                        pass
                    break
            playlist = {
                "playlistTitle": candidate.stem,
                "playlistAuthor": "",
                "image": image_data,
                "customData": {},
                "songs": [
                    {"key": s.song_id, "hash": s.song_hash, "songName": s.display_name}
                    for s in all_picks
                ],
            }
            candidate.parent.mkdir(parents=True, exist_ok=True)
            with open(candidate, "w", encoding="utf-8") as f:
                json.dump(playlist, f, indent=2)
            print(f"Created '{candidate.name}' with {len(all_picks)} random song(s).")
            sys.exit(0)

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

    app = SongBrowser(custom_levels, startup_playlist=playlist_path, startup_random_groups=random_groups)
    app.mainloop()


if __name__ == "__main__":
    main()
