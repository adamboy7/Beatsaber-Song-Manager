import json
import os
import shutil
import tempfile
import webbrowser
import tkinter as tk
import tkinter.filedialog as fd
from pathlib import Path
from tkinter import messagebox

from libraries.song_data import SongInfo
from libraries.asset_editor import bak_files, restore_files, replace_art, replace_audio
from libraries.audio_utils import find_ffmpeg
from libraries.player_data import song_level_ids, load_player_stats
from libraries.favorites import backup_player_data, confirm_player_data_write, _atomic_write_player_data
from libraries.constants import BG_COLOR, ACCENT_COLOR, TEXT_COLOR


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
        messagebox.showwarning("Replace Art", "This song has no cover image to replace.")
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
        messagebox.showerror("Replace Art Failed", str(exc))
        return False


def prompt_ffmpeg_download(parent: tk.Misc) -> None:
    dlg = tk.Toplevel(parent)
    dlg.title("ffmpeg Required")
    dlg.configure(bg=BG_COLOR)
    dlg.resizable(False, False)
    dlg.grab_set()

    tk.Label(
        dlg,
        text="ffmpeg is required to convert audio files.\n"
             "Download it and add ffmpeg.exe to your PATH.",
        font=("Segoe UI", 10),
        bg=BG_COLOR, fg=TEXT_COLOR,
        padx=24, pady=18,
        justify="center",
    ).pack()

    btn_frame = tk.Frame(dlg, bg=BG_COLOR)
    btn_frame.pack(pady=(0, 16))

    tk.Button(
        btn_frame,
        text="Download",
        font=("Segoe UI", 10),
        bg=ACCENT_COLOR, fg=TEXT_COLOR,
        activebackground="#a01d90", activeforeground=TEXT_COLOR,
        relief="flat", padx=14, pady=5,
        command=lambda: (
            webbrowser.open("https://ffmpeg.org/download.html#build-windows"),
            dlg.destroy(),
        ),
    ).pack(side="left", padx=6)

    tk.Button(
        btn_frame,
        text="Cancel",
        font=("Segoe UI", 10),
        bg="#333333", fg=TEXT_COLOR,
        activebackground="#444444", activeforeground=TEXT_COLOR,
        relief="flat", padx=14, pady=5,
        command=dlg.destroy,
    ).pack(side="left", padx=6)

    dlg.wait_window()


def replace_song_audio(parent: tk.Misc, song: SongInfo) -> bool:
    """Open file dialog and replace audio file. Returns True if replaced."""
    if not song.audio_path:
        messagebox.showwarning("Replace Audio", "This song has no audio file to replace.")
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
        messagebox.showerror("Replace Audio Failed", str(exc))
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
        is_v4 = data.get("version", "").startswith("4")
        if is_v4:
            if "song" not in data:
                data["song"] = {}
            data["song"]["title"] = song_name
            data["song"]["author"] = author
            new_mappers = [mapper] if mapper else []
            for bm in data.get("difficultyBeatmaps", []):
                authors = bm.setdefault("beatmapAuthors", {})
                existing = authors.get("mappers", [])
                if existing:
                    existing[0] = mapper if mapper else ""
                    if not mapper:
                        existing.clear()
                else:
                    authors["mappers"] = new_mappers[:]
        else:
            data["_songName"] = song_name
            data["_songAuthorName"] = author
            data["_levelAuthorName"] = mapper
        bak = info_file.parent / (info_file.name + ".bak")
        if not bak.exists():
            shutil.copy2(info_file, bak)
        content = json.dumps(data, ensure_ascii=False, indent=2)
        fd, tmp_str = tempfile.mkstemp(dir=str(info_file.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_str, info_file)
        except:
            Path(tmp_str).unlink(missing_ok=True)
            raise
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
    return None


def clear_song_score(player_dat_path: Path, song: SongInfo) -> tuple[int, dict] | None:
    """Delete all score entries for song. Returns (removed_count, new_stats) or None on failure."""
    ids_to_clear = set(song_level_ids(song))
    if not confirm_player_data_write():
        return None
    try:
        mtime_before = player_dat_path.stat().st_mtime
        raw = player_dat_path.read_text(encoding="utf-8", errors="replace")
        backup_player_data(player_dat_path, raw)
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
        return removed, load_player_stats(player_dat_path)
    except Exception as exc:
        messagebox.showerror("Clear Score Failed", str(exc))
        return None
