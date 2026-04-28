import json
import webbrowser
import tkinter as tk
import tkinter.filedialog as fd
from pathlib import Path
from tkinter import messagebox

from libraries.song_data import SongInfo
from libraries.asset_editor import bak_files, restore_files, replace_art, replace_audio
from libraries.audio_utils import find_ffmpeg
from libraries.player_data import song_level_ids, load_player_stats
from libraries.favorites import backup_player_data
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


def clear_song_score(player_dat_path: Path, song: SongInfo) -> tuple[int, dict] | None:
    """Delete all score entries for song. Returns (removed_count, new_stats) or None on failure."""
    ids_to_clear = set(song_level_ids(song))
    try:
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
        player_dat_path.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        return removed, load_player_stats(player_dat_path)
    except Exception as exc:
        messagebox.showerror("Clear Score Failed", str(exc))
        return None
