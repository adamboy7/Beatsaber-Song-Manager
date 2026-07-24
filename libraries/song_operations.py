import json
import shutil
import webbrowser
import tkinter as tk
import tkinter.filedialog as fd
from pathlib import Path
from libraries import dialogs

from libraries.song_data import SongInfo
from libraries.asset_editor import bak_files, restore_files, replace_art, replace_audio
from libraries.audio_utils import find_ffmpeg
from libraries.fs_utils import atomic_write_text
from libraries.player_data import song_level_ids, load_player_stats
from libraries.favorites import backup_player_data, confirm_player_data_write, _atomic_write_player_data


def restore_song_files(song: SongInfo) -> tuple[int, list[str]]:
    """Restore backup files in the song folder. Returns (bak_count, errors)."""
    baks = bak_files(song)
    if not baks:
        return 0, []
    errors = restore_files(song)
    return len(baks), errors


def replace_song_art(parent: tk.Misc, song: SongInfo) -> bool:
    """Open file dialog and replace cover art. Returns True if replaced."""
    if not song.cover_path:
        dialogs.show_warning("Replace Art", "This song has no cover image to replace.")
        return False
    new_path_str = fd.askopenfilename(
        title="Select New Cover Image",
        filetypes=[("Image files", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"), ("All files", "*.*")],
    )
    if not new_path_str:
        return False
    try:
        replace_art(song.cover_path, new_path_str)
        return True
    except Exception as exc:
        dialogs.show_error("Replace Art Failed", str(exc))
        return False


def prompt_ffmpeg_download(parent: tk.Misc) -> None:
    choice = dialogs.ask_custom(
        "ffmpeg Required",
        "ffmpeg is required to convert audio files.\n"
        "Download it and add ffmpeg.exe to your PATH.",
        buttons=[("Download", "download"), ("Cancel", "")],
        parent=parent,
        default="",
    )
    if choice == "download":
        webbrowser.open("https://ffmpeg.org/download.html#build-windows")


def replace_song_audio(parent: tk.Misc, song: SongInfo) -> bool:
    """Open file dialog and replace audio file. Returns True if replaced."""
    if not song.audio_path:
        dialogs.show_warning("Replace Audio", "This song has no audio file to replace.")
        return False
    new_path_str = fd.askopenfilename(
        title="Select New Audio File",
        filetypes=[
            ("Audio files", "*.mp3 *.wav *.ogg *.egg *.m4a"),
            ("MP3",         "*.mp3"),
            ("WAV",         "*.wav"),
            ("OGG / EGG",   "*.ogg *.egg"),
            ("M4A",         "*.m4a"),
            ("All files",   "*.*"),
        ],
    )
    if not new_path_str:
        return False
    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path and Path(new_path_str).suffix.lower() not in (".egg", ".ogg"):
        prompt_ffmpeg_download(parent)
        return False
    try:
        replace_audio(song.audio_path, new_path_str, ffmpeg_path or "")
        return True
    except Exception as exc:
        dialogs.show_error("Replace Audio Failed", str(exc))
        return False


def save_song_info(song: SongInfo, song_name: str, author: str, mapper: str) -> str | None:
    """Write song name, artist, and mapper back to Info.dat. Returns error string or None."""
    info_file = None
    for name in ("Info.dat", "info.dat", "INFO.DAT"):
        candidate = song.folder / name
        if candidate.exists():
            info_file = candidate
            break
    if info_file is None:
        return "Info.dat not found in song folder."
    try:
        data = json.loads(info_file.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        return f"Failed to read Info.dat: {exc}"
    try:
        is_v4 = str(data.get("version", "")).startswith("4")
        if is_v4:
            if "song" not in data:
                data["song"] = {}
            data["song"]["title"] = song_name
            data["song"]["author"] = author
            new_mappers = [m.strip() for m in mapper.split(",") if m.strip()] if mapper else []
            for bm in data.get("difficultyBeatmaps", []):
                authors = bm.setdefault("beatmapAuthors", {})
                authors["mappers"] = new_mappers[:]
        else:
            data["_songName"] = song_name
            data["_songAuthorName"] = author
            data["_levelAuthorName"] = mapper
        bak = info_file.parent / (info_file.name + ".bak")
        if not bak.exists():
            shutil.copy2(info_file, bak)
        content = json.dumps(data, ensure_ascii=False, indent=2)
        atomic_write_text(info_file, content)
    except Exception as exc:
        return f"Failed to write Info.dat: {exc}"

    song.song_name = song_name
    song.author = author
    song.mapper = mapper
    if song_name:
        song.display_name = song_name
        if song.sub_name:
            song.display_name += f" {song.sub_name}"
    else:
        song.display_name = song.folder.name
    song.update_search_blob()
    return None


def clear_song_score(player_dat_path: Path, song: SongInfo) -> tuple[int, dict] | None:
    """Delete all score entries for song. Returns (removed_count, new_stats) or None on failure."""
    ids_to_clear = set(song_level_ids(song))
    if not confirm_player_data_write():
        return None
    try:
        mtime_before = player_dat_path.stat().st_mtime_ns
        raw = player_dat_path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
        players = data.get("localPlayers", [])
        if not players:
            return None
        entries = players[0].get("levelsStatsData", [])
        before = len(entries)
        players[0]["levelsStatsData"] = [
            e for e in entries if e.get("levelId", "") not in ids_to_clear
        ]
        removed = before - len(players[0]["levelsStatsData"])
        content = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if not _atomic_write_player_data(player_dat_path, content, mtime_before):
            return None
        # Only back up after a successful write, so an aborted operation
        # doesn't leave stray `.dat.bak` files behind.
        backup_player_data(player_dat_path, raw)
        return removed, load_player_stats(player_dat_path)
    except Exception as exc:
        dialogs.show_error("Clear Score Failed", str(exc))
        return None
