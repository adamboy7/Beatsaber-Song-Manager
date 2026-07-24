import os
import shutil
import subprocess
import tempfile
from pathlib import Path


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
    except BaseException:
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
            if not ffmpeg_path:
                raise RuntimeError(
                    "ffmpeg not available — cannot convert audio.\n"
                    "Place ffmpeg.exe next to this script or add it to PATH."
                )
            # Invoke ffmpeg directly with CREATE_NO_WINDOW rather than going
            # through pydub. Avoids the global `subprocess.Popen` monkey-patch
            # that would otherwise be needed to suppress the conversion's
            # console window.
            creation_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            cmd = [
                ffmpeg_path, "-y", "-i", str(new_path),
                "-c:a", "libvorbis", "-q:a", "5", "-f", "ogg", tmp_str,
            ]
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=creation_flag,
            )
            if result.returncode != 0:
                err = (result.stderr or b"").decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"ffmpeg conversion failed (exit {result.returncode}):\n{err.strip()}"
                )
        bak = audio_path.parent / (audio_path.name + ".bak")
        if not bak.exists():
            shutil.copy2(audio_path, bak)
        os.replace(tmp_str, audio_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
