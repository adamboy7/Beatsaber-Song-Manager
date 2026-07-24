import json
import shutil
import subprocess
import sys
from pathlib import Path


def _local_dir() -> Path:
    # When frozen by PyInstaller, look next to the EXE, not in the temp bundle.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent.parent


# Cache only *successful* lookups. A permanent cache (e.g. lru_cache) would pin
# a startup-time miss forever, so dropping ffmpeg.exe next to the app wouldn't be
# picked up until a restart. By caching only a found path, a miss keeps re-probing
# on each call and detects a newly added binary on the fly — no reload needed.
_ffmpeg_cache: str | None = None
_ffprobe_cache: str | None = None


def _resolve(name: str) -> str | None:
    """Locate a binary: check the app/script directory first, then PATH."""
    local = _local_dir() / f"{name}.exe"
    if local.exists():
        return str(local)
    return shutil.which(name)


def find_ffmpeg() -> str | None:
    """Return path to ffmpeg: checks app directory first, then PATH.

    Re-probes on every call until found, so an ffmpeg.exe placed beside the app
    after launch is detected without restarting.
    """
    global _ffmpeg_cache
    if _ffmpeg_cache is None:
        _ffmpeg_cache = _resolve("ffmpeg")
    return _ffmpeg_cache


def find_ffprobe() -> str | None:
    """Return path to ffprobe: checks app directory first, then PATH.

    Re-probes on every call until found, so an ffprobe.exe placed beside the app
    after launch is detected without restarting.
    """
    global _ffprobe_cache
    if _ffprobe_cache is None:
        _ffprobe_cache = _resolve("ffprobe")
    return _ffprobe_cache


def get_audio_duration(path: Path) -> float | None:
    """Return audio duration in seconds using ffprobe, or None if unavailable."""
    ffprobe = find_ffprobe()
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams", str(path)],
            capture_output=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        data = json.loads(result.stdout.decode("utf-8", errors="replace"))
        for stream in data.get("streams", []):
            dur = stream.get("duration")
            if dur:
                return float(dur)
    except Exception:
        pass
    return None
