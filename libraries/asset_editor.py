import contextlib
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path


# Thread-local flag toggled by `_no_console_window`. The monkey-patched Popen
# subclass only applies CREATE_NO_WINDOW when *this* thread has the patch
# active, so background threads (pynput listener, install watchers, audio
# Popen, the loopback HTTP server) constructing a Popen concurrently are not
# affected.
_quiet_local = threading.local()
_patch_lock = threading.Lock()
_patch_depth = 0
_orig_popen = subprocess.Popen


class _Quiet(_orig_popen):
    def __init__(self, *args, **kwargs):
        if getattr(_quiet_local, "active", False):
            flags = kwargs.get("creationflags", 0) or 0
            creation_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            kwargs["creationflags"] = flags | creation_flag
        super().__init__(*args, **kwargs)


@contextlib.contextmanager
def _no_console_window():
    """Suppress Windows console windows for subprocess.Popen calls in *this* thread.

    The patch is reference-counted across threads so concurrent users don't
    each toggle the global Popen back to the original prematurely; the
    creationflags override is scoped to the calling thread via a thread-local.
    """
    global _patch_depth
    with _patch_lock:
        if _patch_depth == 0:
            subprocess.Popen = _Quiet  # type: ignore[misc]
        _patch_depth += 1
    _quiet_local.active = True
    try:
        yield
    finally:
        _quiet_local.active = False
        with _patch_lock:
            _patch_depth -= 1
            if _patch_depth == 0:
                subprocess.Popen = _orig_popen  # type: ignore[misc]

from libraries.song_data import SongInfo

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore[assignment]


def bak_files(song: SongInfo) -> list[Path]:
    """Return all .bak files in the song folder."""
    return list(song.folder.glob("*.bak"))


def restore_files(song: SongInfo) -> list[str]:
    """Move every .bak file back to its original name. Returns error strings for any failures."""
    errors = []
    for bak in bak_files(song):
        dest = bak.with_suffix("")
        try:
            shutil.move(str(bak), str(dest))
        except Exception as exc:
            errors.append(f"{bak.name}: {exc}")
    return errors


def replace_art(cover_path: Path, new_path: str) -> None:
    """Backup the current cover image and replace it with new_path, resized to match the original."""
    with Image.open(cover_path) as orig:
        orig_size = orig.size
        orig_format = orig.format or cover_path.suffix.lstrip(".").upper()
        if orig_format == "JPG":
            orig_format = "JPEG"

    fd, tmp_str = tempfile.mkstemp(dir=str(cover_path.parent), suffix=".tmp")
    tmp = Path(tmp_str)
    try:
        os.close(fd)
        with Image.open(new_path) as new_img:
            if orig_format == "JPEG":
                new_img = new_img.convert("RGB")
            new_img = new_img.resize(orig_size, Image.LANCZOS)
            new_img.save(tmp_str, format=orig_format)
        bak = cover_path.parent / (cover_path.name + ".bak")
        if not bak.exists():
            shutil.copy2(cover_path, bak)
        os.replace(tmp_str, cover_path)
    except:
        tmp.unlink(missing_ok=True)
        raise


def replace_audio(audio_path: Path, new_path: str, ffmpeg_path: str) -> None:
    """Backup the current audio file and replace it with new_path, converting to OGG if needed."""
    new = Path(new_path)
    ext = new.suffix.lower()

    fd, tmp_str = tempfile.mkstemp(dir=str(audio_path.parent), suffix=".tmp")
    tmp = Path(tmp_str)
    try:
        os.close(fd)
        if ext in (".egg", ".ogg"):
            shutil.copy2(new, tmp_str)
        else:
            try:
                from pydub import AudioSegment
            except ImportError as e:
                raise RuntimeError(
                    "Missing dependencies for audio conversion.\n"
                    "Run: pip install -r requirements.txt\n\n"
                    f"Detail: {e}"
                ) from e
            AudioSegment.converter = ffmpeg_path
            with _no_console_window():
                audio = AudioSegment.from_file(new_path)
                audio.export(tmp_str, format="ogg")
        bak = audio_path.parent / (audio_path.name + ".bak")
        if not bak.exists():
            shutil.copy2(audio_path, bak)
        os.replace(tmp_str, audio_path)
    except:
        tmp.unlink(missing_ok=True)
        raise
