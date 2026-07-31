"""Install a single Beat Saber map by downloading it directly from BeatSaver.

The download runs in a background thread and extracts into ``CustomLevels``,
so completion is known exactly. There is deliberately no cancel: a download in
flight can't be aborted (the files land on disk regardless), so completions
are always honored — and on app shutdown they're dispatched to a dead pump
and simply never run.
"""

from __future__ import annotations

import threading
from pathlib import Path

from libraries import beatsaver_api as bs


class InstallManager:
    def __init__(self, custom_levels: Path, dispatch_fn, status_cb, reload_cb):
        self.custom_levels = custom_levels
        self._dispatch = dispatch_fn
        self._status_cb = status_cb
        self._reload_cb = reload_cb
        self._active_ids: set[str] = set()

    def trigger(self, song_id: str) -> None:
        song_id = (song_id or "").strip().lower()
        if not song_id or song_id in self._active_ids:
            return  # already downloading this song; ignore the repeat click
        self._active_ids.add(song_id)
        self._status_cb(f"Downloading {song_id}…")
        threading.Thread(
            target=self._worker, args=(song_id,), daemon=True
        ).start()

    def _worker(self, song_id: str) -> None:
        try:
            bs.install_song(song_id, self.custom_levels)
        except Exception as e:  # noqa: BLE001 - report any failure to the UI
            self._dispatch(lambda exc=e: self._on_error(song_id, exc))
            return
        self._dispatch(lambda: self._on_complete(song_id))

    def _on_complete(self, song_id: str) -> None:
        # Discard here (not in _worker) so the active-id set is only ever
        # touched from the main thread, like the rest of this class's state.
        self._active_ids.discard(song_id)
        self._status_cb(f"Installed {song_id}.")
        self._reload_cb()

    def _on_error(self, song_id: str, err: Exception) -> None:
        self._active_ids.discard(song_id)
        self._status_cb(f"Could not install {song_id}: {err}")
        self._reload_cb()
