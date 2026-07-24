"""Install a single Beat Saber map by downloading it directly from BeatSaver.

The download runs in a background thread and extracts into ``CustomLevels``,
so completion is known exactly.
"""

from __future__ import annotations

import threading
from pathlib import Path

from libraries import beatsaver_api as bs


class InstallManager:
    def __init__(self, custom_levels: Path, after_fn, status_cb, reload_cb):
        self.custom_levels = custom_levels
        self._after = after_fn
        self._status_cb = status_cb
        self._reload_cb = reload_cb
        self._gen = 0
        self._active_ids: set[str] = set()

    def cancel(self) -> None:
        self._gen += 1

    @staticmethod
    def has_handler() -> bool:
        """Installs are always available (no external prerequisites)."""
        return True

    def _dispatch(self, callback) -> None:
        """Schedule callback on the host's event loop, swallowing errors from a
        torn-down UI (e.g. tk.TclError after the window closed mid-install)."""
        try:
            self._after(0, callback)
        except Exception:
            pass

    def trigger(self, song_id: str) -> None:
        song_id = (song_id or "").strip().lower()
        if not song_id or song_id in self._active_ids:
            return  # already downloading this song; ignore the repeat click
        self._active_ids.add(song_id)
        self._gen += 1
        gen = self._gen
        self._status_cb(f"Downloading {song_id}…")
        threading.Thread(
            target=self._worker, args=(song_id, gen), daemon=True
        ).start()

    def _worker(self, song_id: str, gen: int) -> None:
        try:
            try:
                bs.install_song(song_id, self.custom_levels)
            except Exception as e:  # noqa: BLE001 - report any failure to the UI
                if gen == self._gen:
                    self._dispatch(lambda: self._on_error(song_id, e))
                return
            if gen == self._gen:
                self._dispatch(lambda: self._on_complete(song_id, gen))
        finally:
            self._active_ids.discard(song_id)

    def _on_complete(self, song_id: str, gen: int) -> None:
        if gen != self._gen:
            return
        self._gen += 1
        self._status_cb(f"Installed {song_id}.")
        self._reload_cb()

    def _on_error(self, song_id: str, err: Exception) -> None:
        self._gen += 1
        self._status_cb(f"Could not install {song_id}: {err}")
        self._reload_cb()
