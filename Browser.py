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
import threading
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
from libraries.visualizer_window import VisualizerWindow
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
    def __init__(self, custom_levels: Path, startup_playlist: Path | None = None, startup_random_groups: list[tuple[int, str | None]] | None = None, startup_shuffle: bool = False):
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
            self._icon = _icon
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
        self._visualizer_window: VisualizerWindow | None = None
        self._playlist_art_b64: str | None = None
        self._playlist_art_locked: bool = False
        self._playlist_art_first_song_key: str | None = None
        self._playlist_art_window: PlaylistArtWindow | None = None
        self._pending_playlist_entries: list[dict] | None = None
        self._pending_playlist_queue: list[dict] = []
        self._startup_playlist: Path | None = startup_playlist
        self._startup_random_groups: list[tuple[int, str | None]] = startup_random_groups or []
        self._startup_shuffle: bool = startup_shuffle

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

    # Move any playlist path to the front so --randomAdd's greedy nargs='+' can't consume it.
    # Partition by index (not value) so a non-playlist token that happens to match a
    # playlist token's string isn't accidentally pulled out with it.
    _playlist_toks: list[str] = []
    _other_toks: list[str] = []
    for _tok in sys.argv[1:]:
        if not _tok.startswith('-') and Path(_tok).suffix.lower() in {'.bplist', '.json'}:
            _playlist_toks.append(_tok)
        else:
            _other_toks.append(_tok)
    sys.argv = [sys.argv[0]] + _playlist_toks + _other_toks

    parser = argparse.ArgumentParser(
        description="Beat Saber Song Manager",
        epilog=(
            "Flag precedence:\n"
            "  --install short-circuits --shuffle and --randomAdd (both ignored).\n"
            "  --shuffle and --randomAdd may be combined: picks are appended,\n"
            "  then the result is shuffled and written back.\n"
            "\n"
            "Headless examples (exit without launching the GUI):\n"
            "  Browser.py playlist.bplist --install\n"
            "      Install every missing song in playlist.bplist via Mod Assistant\n"
            "      and exit.  Requires Mod Assistant with playlist one-click installs\n"
            "      enabled (bsplaylist:// protocol handler).\n"
            "\n"
            "  Browser.py playlist.bplist --shuffle\n"
            "      Shuffle the playlist in-place and exit.\n"
            "\n"
            "  Browser.py playlist.bplist --randomAdd 10 \"{difficulty}:expertplus\"\n"
            "      Append 10 random songs to the existing playlist and exit.\n"
            "      If playlist.bplist does not exist yet, it is created with the picks.\n"
            "\n"
            "  Browser.py new.bplist --randomAdd 20 --shuffle\n"
            "      Create new.bplist with 20 random songs, shuffled, and exit.\n"
            "\n"
            "GUI examples (launch the browser window):\n"
            "  Browser.py playlist.bplist\n"
            "      Open the browser with the given playlist loaded into the queue.\n"
            "\n"
            "  Browser.py --randomAdd 20 \"{favorite}:y\" [--shuffle]\n"
            "      Open the browser with 20 random favorites as the initial queue.\n"
            "      No file is written; --shuffle (optional) shuffles the queue."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("playlist", nargs="?", help="Playlist file (.bplist / .json)")
    parser.add_argument(
        "--install",
        action="store_true",
        help=(
            "Headless: hand PLAYLIST to Mod Assistant via bsplaylist://, wait for "
            "all missing songs to download, then exit.  Takes precedence over "
            "--shuffle and --randomAdd (both are ignored when --install is set). "
            "Exit code 0 on success, 1 on failure or if the bsplaylist:// handler "
            "is not registered."
        ),
    )
    parser.add_argument(
        "--shuffle",
        action="store_true",
        help=(
            "Shuffle song order.  With a playlist arg this is headless: shuffle "
            "the playlist's songs (after appending any --randomAdd picks) and "
            "write the playlist back to disk.  Without a playlist arg, shuffle "
            "the GUI startup queue (built from --randomAdd).  Requires either a "
            "playlist file or --randomAdd."
        ),
    )
    parser.add_argument(
        "--randomAdd",
        nargs='+',
        action='append',
        metavar=("N", "FILTER"),
        help=(
            "Add N random songs from your library (optional inline filters). "
            "With a playlist arg this is headless: picks are appended to an "
            "existing playlist or written to a new playlist, then exit.  Without "
            "a playlist arg, the picks become the GUI's startup queue (nothing "
            "is written to disk).  May be used multiple times."
        ),
    )
    args = parser.parse_args()

    # --install takes precedence; warn if the user combined it with ignored flags.
    if args.install and (args.shuffle or args.randomAdd):
        print(
            "Note: --install takes precedence; --shuffle and --randomAdd are ignored.",
            file=sys.stderr,
        )

    def _parse_random_groups(raw_groups):
        if not raw_groups:
            return []
        groups = []
        for parts in raw_groups:
            try:
                count = int(parts[0])
            except ValueError:
                print(
                    f"--randomAdd: expected a positive integer count as the first argument, "
                    f"got {parts[0]!r}.\n"
                    "Usage: --randomAdd N [FILTER ...]",
                    file=sys.stderr,
                )
                sys.exit(1)
            if count < 1:
                print(
                    f"--randomAdd: count must be a positive integer, got {count}.",
                    file=sys.stderr,
                )
                sys.exit(1)
            filters = [p.strip(',') for p in parts[1:] if p.strip(',')]
            filter_str = ' '.join(filters) if filters else None
            groups.append((count, filter_str))
        return groups

    random_groups = _parse_random_groups(args.randomAdd)

    # Resolve playlist arg into two values:
    #   playlist_arg  — the raw Path, if one was given (file may or may not exist)
    #   playlist_path — the same Path, but only when it points at an existing
    #                   .bplist/.json file (used by headless --install and the
    #                   GUI "load playlist into queue" startup hook)
    playlist_arg: Path | None = Path(args.playlist) if args.playlist else None
    playlist_has_valid_suffix = (
        playlist_arg is not None
        and playlist_arg.suffix.lower() in {".bplist", ".json"}
    )
    playlist_path: Path | None = (
        playlist_arg if (playlist_has_valid_suffix and playlist_arg.is_file()) else None
    )

    if args.install:
        if playlist_path is None:
            print("--install requires a valid .bplist or .json playlist file.")
            sys.exit(1)
        if not PlaylistInstaller.has_handler():
            print(
                "bsplaylist:// handler not found — install Mod Assistant and "
                "enable playlist one-click installs, then retry."
            )
            sys.exit(1)
        custom_levels = find_beatsaber_custom_levels()
        if custom_levels is None:
            print("Beat Saber not found automatically; cannot determine CustomLevels path.")
            sys.exit(1)
        with open(playlist_path, "r", encoding="utf-8") as f:
            _pl_data = json.load(f)
        expected_keys = [
            (e.get("key") or e.get("id") or "")
            for e in _pl_data.get("songs", [])
        ]

        _done = threading.Event()
        _success: list[bool] = [False]

        def _headless_after(ms, callback):
            threading.Timer(ms / 1000, callback).start()

        def _on_complete(success: bool):
            _success[0] = success
            _done.set()

        installer = PlaylistInstaller(
            custom_levels,
            _headless_after,
            print,
            _on_complete,
        )
        if not installer.install(playlist_path, expected_keys):
            # install() returned False without scheduling _complete_cb
            # (e.g. handler missing, playlist vanished, or server start
            # failed). Don't block on _done forever.
            sys.exit(1)
        _done.wait()
        sys.exit(0 if _success[0] else 1)

    # --shuffle requires either an existing playlist file or --randomAdd to
    # supply songs.  Drops the old GUI-startup-shuffle-without-anything-to-shuffle
    # silent no-op.
    if args.shuffle and not playlist_path and not random_groups:
        if playlist_arg is not None and playlist_has_valid_suffix:
            # User supplied a playlist arg that just doesn't exist yet.
            print(
                f"--shuffle: '{playlist_arg}' does not exist; combine with --randomAdd "
                "to create it."
            )
        else:
            print("--shuffle requires either an existing playlist file or --randomAdd.")
        sys.exit(1)

    # ── Headless: --shuffle and/or --randomAdd with a playlist file ──────────
    # File may already exist (read-modify-write) or be new (create-and-write).
    # Replaces the previous two separate branches (existing-file shuffle and
    # new-file randomAdd) which silently dropped flags between them.
    if playlist_has_valid_suffix and (args.shuffle or random_groups):
        existing_file = playlist_path is not None
        if existing_file:
            with open(playlist_arg, "r", encoding="utf-8") as f:
                data = json.load(f)
            playlist_songs = data.get("songs")
            if not isinstance(playlist_songs, list):
                print("Playlist has no 'songs' array.")
                sys.exit(1)
        else:
            data = {
                "playlistTitle": playlist_arg.stem,
                "playlistAuthor": "",
                "image": "",
                "customData": {},
                "songs": [],
            }
            playlist_songs = data["songs"]

        added_count = 0
        first_pick = None
        if random_groups:
            custom_levels = find_beatsaber_custom_levels()
            if custom_levels is None:
                print("--randomAdd requires Beat Saber to be found automatically.")
                sys.exit(1)
            library = load_songs(custom_levels)
            hashes = load_song_hashes(custom_levels)
            for song in library:
                song.song_hash = hashes.get(song.folder.name, "")
            existing_hashes = {(e.get("hash") or "").upper() for e in playlist_songs}
            remaining_candidates = [
                s for s in library
                if s.song_hash and s.song_hash.upper() not in existing_hashes
            ]
            if not remaining_candidates:
                print("Warning: no candidate songs available; skipping --randomAdd.")
            else:
                ps, fi = _load_player_data_headless()
                for count, filter_str in random_groups:
                    filtered_candidates = None
                    if filter_str:
                        filtered_candidates = filter_songs(
                            remaining_candidates, filter_str, ps, fi
                        )
                        if not filtered_candidates:
                            print(
                                f"Warning: filter '{filter_str}' matched no songs; "
                                "falling back to unfiltered picks."
                            )
                    picks = pick_random_songs(
                        filtered_candidates, remaining_candidates, count
                    )
                    for song in picks:
                        if first_pick is None:
                            first_pick = song
                        playlist_songs.append({
                            "key": song.song_id,
                            "hash": song.song_hash,
                            "songName": song.display_name,
                        })
                    picked_folders = {s.folder for s in picks}
                    remaining_candidates = [
                        s for s in remaining_candidates if s.folder not in picked_folders
                    ]
                    added_count += len(picks)

        if args.shuffle:
            random.shuffle(playlist_songs)

        # New playlists: derive cover art from the first pick when available.
        if not existing_file and not data.get("image") and first_pick is not None:
            if first_pick.cover_path and first_pick.cover_path.exists():
                try:
                    from PIL import Image
                    buf = io.BytesIO()
                    Image.open(first_pick.cover_path).convert("RGB").save(buf, format="JPEG")
                    data["image"] = base64.b64encode(buf.getvalue()).decode("ascii")
                except Exception:
                    pass

        data["songs"] = playlist_songs
        playlist_arg.parent.mkdir(parents=True, exist_ok=True)
        with open(playlist_arg, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        if not existing_file:
            shuf = " (shuffled)" if args.shuffle else ""
            print(f"Created '{playlist_arg.name}' with {len(playlist_songs)} song(s){shuf}.")
        else:
            parts = []
            if added_count:
                parts.append(f"appended {added_count} song(s)")
            if args.shuffle:
                parts.append(f"shuffled {len(playlist_songs)} songs")
            summary = " and ".join(parts) if parts else "no changes"
            print(f"{summary.capitalize()} in '{playlist_arg.name}'.")
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

    app = SongBrowser(custom_levels, startup_playlist=playlist_path, startup_random_groups=random_groups, startup_shuffle=args.shuffle)
    app.mainloop()


if __name__ == "__main__":
    main()
