import json
import time
import threading
import webbrowser
from pathlib import Path


class InstallManager:
    def __init__(self, custom_levels: Path, after_fn, status_cb, reload_cb):
        self.custom_levels = custom_levels
        self._after = after_fn
        self._status_cb = status_cb
        self._reload_cb = reload_cb
        self._gen = 0

    def cancel(self) -> None:
        self._gen += 1

    def trigger(self, song_id: str) -> None:
        if not self.has_handler():
            self._status_cb(
                "No handler for beatsaver:// — install Mod Assistant and enable one-click installs."
            )
            return
        webbrowser.open(f"beatsaver://{song_id}")
        self._watch(song_id)

    @staticmethod
    def has_handler() -> bool:
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "beatsaver")
            winreg.CloseKey(key)
            return True
        except (FileNotFoundError, OSError):
            return False

    def _watch(self, song_id: str) -> None:
        self._gen += 1
        gen = self._gen
        self._pulse(song_id, gen, 0)

        def worker():
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if gen != self._gen:
                    return
                try:
                    for entry in self.custom_levels.iterdir():
                        if not entry.is_dir():
                            continue
                        name = entry.name.lower()
                        if name.startswith(song_id + " ") or name == song_id:
                            if self._is_complete(entry):
                                if gen == self._gen:
                                    self._after(0, lambda: self._on_complete(gen))
                                return
                except Exception:
                    pass
                time.sleep(1)
            if gen == self._gen:
                self._after(0, lambda: self._on_timeout(song_id, gen))

        threading.Thread(target=worker, daemon=True).start()

    def _pulse(self, song_id: str, gen: int, elapsed: int) -> None:
        if gen != self._gen:
            return
        dots = "." * (elapsed % 4)
        self._status_cb(f"Waiting for {song_id} to install{dots}  ({elapsed}s)")
        if elapsed < 30:
            self._after(1000, lambda: self._pulse(song_id, gen, elapsed + 1))

    @staticmethod
    def _is_complete(folder: Path) -> bool:
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

    def _on_complete(self, gen: int) -> None:
        if gen != self._gen:
            return
        self._gen += 1
        self._reload_cb()

    def _on_timeout(self, song_id: str, gen: int) -> None:
        if gen != self._gen:
            return
        self._gen += 1
        self._status_cb(
            f"No install detected for {song_id} — check that Mod Assistant is running and one-click installs are enabled."
        )
