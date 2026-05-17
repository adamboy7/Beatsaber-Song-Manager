import json
import shutil
import subprocess
from pathlib import Path


def find_ffmpeg() -> str | None:
    """Return path to ffmpeg: checks script directory first, then PATH."""
    local = Path(__file__).parent.parent / "ffmpeg.exe"
    if local.exists():
        return str(local)
    return shutil.which("ffmpeg")


def find_ffplay() -> str | None:
    """Return path to ffplay: checks script directory first, then PATH."""
    local = Path(__file__).parent.parent / "ffplay.exe"
    if local.exists():
        return str(local)
    return shutil.which("ffplay")


def find_ffprobe() -> str | None:
    """Return path to ffprobe: checks script directory first, then PATH."""
    local = Path(__file__).parent.parent / "ffprobe.exe"
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
        )
        data = json.loads(result.stdout.decode("utf-8", errors="replace"))
        for stream in data.get("streams", []):
            dur = stream.get("duration")
            if dur:
                return float(dur)
    except Exception:
        pass
    return None
