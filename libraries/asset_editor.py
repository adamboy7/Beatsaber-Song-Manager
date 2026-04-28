import shutil
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

    shutil.copy2(cover_path, cover_path.parent / (cover_path.name + ".bak"))

    with Image.open(new_path) as new_img:
        if orig_format == "JPEG":
            new_img = new_img.convert("RGB")
        new_img = new_img.resize(orig_size, Image.LANCZOS)
        new_img.save(cover_path, format=orig_format)


def replace_audio(audio_path: Path, new_path: str, ffmpeg_path: str) -> None:
    """Backup the current audio file and replace it with new_path, converting to OGG if needed."""
    new = Path(new_path)
    ext = new.suffix.lower()

    shutil.copy2(audio_path, audio_path.parent / (audio_path.name + ".bak"))

    if ext in (".egg", ".ogg"):
        shutil.copy2(new, audio_path)
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
        audio = AudioSegment.from_file(new_path)
        audio.export(str(audio_path), format="ogg")
