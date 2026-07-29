"""Install a Beat Saber playlist by downloading its songs directly from
BeatSaver.

Songs are fetched in-process, one per playlist entry, so progress and
completion are exact.

Callers that already know which entries are missing should pass them via
``install(..., entries=...)``: every entry handed to the installer costs one
rate-limited BeatSaver metadata request, even when the map turns out to be
installed already, because the on-disk check needs the folder name from that
response. Filtering up front is the difference between one request per missing
song and one request per song in the playlist.
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

    def cancel(self) -> None:
        self._gen += 1

    def install(
        self,
        playlist_path: Path,
        entries: list[dict] | None = None,
        installed_hashes: set[str] | None = None,
    ) -> bool:
        """Download the songs referenced by ``playlist_path``.

        ``entries`` overrides what gets installed — pass the already-computed
        missing entries to skip both the file read and a metadata request per
        already-installed song. When omitted, every song in the playlist file is
        considered.

        ``installed_hashes`` is an optional set of song hashes (case-insensitive)
        known to be installed; matching entries are dropped before any network
        request. Use it when a pre-filtered ``entries`` list isn't available.

        Returns ``True`` once the background download has been launched;
        completion is reported through ``complete_cb``.
        """
        self.cancel()

        if entries is None:
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

            entries = data.get("songs", []) or []

        songs = [s for s in entries if isinstance(s, dict)]
        if installed_hashes:
            known = {h.upper() for h in installed_hashes if h}
            songs = [
                s for s in songs
                if (s.get("hash") or "").upper() not in known
            ]

        self._gen += 1
        gen = self._gen
        threading.Thread(
            target=self._worker, args=(songs, gen), daemon=True
        ).start()
        return True

    def _worker(self, songs: list[dict], gen: int) -> None:
        total = len(songs)
        downloaded = 0
        already = 0
        failed = 0
        unfetchable = 0

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
                    result = bs.install_by_hash(song_hash, self.custom_levels)
                elif key:
                    result = bs.install_song(key, self.custom_levels)
                else:
                    unfetchable += 1  # no hash and no key: nothing to fetch
                    continue
            except Exception:  # noqa: BLE001 - record and keep going
                failed += 1
                continue

            if result.downloaded:
                downloaded += 1
            else:
                already += 1

        if gen == self._gen:
            self._dispatch(
                lambda: self._on_complete(
                    gen, total, downloaded, already, failed, unfetchable
                )
            )

    def _on_complete(self, gen: int, total: int, downloaded: int,
                     already: int, failed: int, unfetchable: int) -> None:
        if gen != self._gen:
            return
        self._gen += 1
        self._status_cb(self._summary(total, downloaded, already,
                                      failed, unfetchable))
        # Only a clean run counts as success. Skipped and already-present
        # entries are not failures; a download that raised is.
        self._complete_cb(failed == 0)

    @staticmethod
    def _summary(total: int, downloaded: int, already: int,
                 failed: int, unfetchable: int) -> str:
        if total == 0:
            return "Playlist install finished — nothing to download."
        parts = [f"{downloaded} downloaded"]
        if already:
            parts.append(f"{already} already installed")
        if failed:
            parts.append(f"{failed} failed")
        if unfetchable:
            parts.append(f"{unfetchable} skipped (no BeatSaver key)")
        return f"Playlist install finished — {', '.join(parts)} of {total}."
