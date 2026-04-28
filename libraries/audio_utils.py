import shutil
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
