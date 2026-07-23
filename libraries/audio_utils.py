import functools
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


@functools.lru_cache(maxsize=1)
def find_ffmpeg() -> str | None:
    """Return path to ffmpeg: checks script directory first, then PATH."""
    local = _local_dir() / "ffmpeg.exe"
    if local.exists():
        return str(local)
    return shutil.which("ffmpeg")


@functools.lru_cache(maxsize=1)
def find_ffprobe() -> str | None:
    """Return path to ffprobe: checks script directory first, then PATH."""
    local = _local_dir() / "ffprobe.exe"
    if local.exists():
        return str(local)
    return shutil.which("ffprobe")


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
