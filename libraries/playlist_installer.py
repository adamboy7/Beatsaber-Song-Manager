"""Install a Beat Saber playlist by downloading each of its songs directly
from BeatSaver.

Songs are fetched in-process, one per playlist entry, so progress and
completion are exact.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from libraries import beatsaver_api as bs


class PlaylistInstaller:
    """One-shot playlist installer.

    The host wires ``dispatch_fn`` to a thread-safe dispatcher (see
    ``libraries.tk_dispatch``) so all callbacks run on the UI thread.
    ``status_cb`` receives short progress strings, and
    ``complete_cb(success: bool)`` is invoked once when the install finishes or
    is cancelled.
    """

    def __init__(self, custom_levels: Path, dispatch_fn, status_cb, complete_cb):
        self.custom_levels = custom_levels
        self._dispatch = dispatch_fn
        self._status_cb = status_cb
        self._complete_cb = complete_cb
        self._gen = 0

    @staticmethod
    def has_handler() -> bool:
        """Playlist installs are always available (no external prerequisites)."""
        return True

    def cancel(self) -> None:
        self._gen += 1

    def install(self, playlist_path: Path) -> bool:
        """Download every song referenced by ``playlist_path``.

        Returns ``True`` once the background download has been launched;
        completion is reported through ``complete_cb``.
        """
        self.cancel()

        playlist_path = Path(playlist_path)
        if not playlist_path.is_file():
            self._status_cb(f"Playlist not found: {playlist_path}")
            return False

        try:
            data = json.loads(
                playlist_path.read_text(encoding="utf-8", errors="replace")
            )
        except Exception as e:  # noqa: BLE001
            self._status_cb(f"Could not read playlist: {e}")
            return False

        songs = data.get("songs", []) or []
        self._gen += 1
        gen = self._gen
        threading.Thread(
            target=self._worker, args=(songs, gen), daemon=True
        ).start()
        return True

    def _worker(self, songs: list[dict], gen: int) -> None:
        total = len(songs)
        installed = 0

        for index, song in enumerate(songs, start=1):
            if gen != self._gen:
                return  # cancelled / superseded

            song_hash = song.get("hash")
            key = song.get("key") or song.get("id")
            label = song.get("songName") or key or song_hash or "song"

            self._dispatch(
                lambda i=index, t=total, lbl=label: self._status_cb(
                    f"Installing playlist — {i}/{t}: {lbl}"
                )
            )

            try:
                if song_hash:
                    bs.install_by_hash(song_hash, self.custom_levels)
                elif key:
                    bs.install_song(key, self.custom_levels)
                else:
                    continue  # hash-less and key-less entry: nothing to fetch
                installed += 1
            except Exception:  # noqa: BLE001 - skip failures, keep going
                continue

        if gen == self._gen:
            self._dispatch(lambda: self._on_complete(gen, installed, total))

    def _on_complete(self, gen: int, installed: int, total: int) -> None:
        if gen != self._gen:
            return
        self._gen += 1
        self._status_cb(f"Playlist install finished — {installed}/{total} songs.")
        # Success if we installed anything, or if there was nothing to install.
        self._complete_cb(installed > 0 or total == 0)
