import json
import os
import subprocess
import tempfile
import datetime
from pathlib import Path
from tkinter import messagebox

from libraries.song_data import SongInfo


def favorite_level_id(song: SongInfo) -> str:
    if song.song_hash:
        return f"custom_level_{song.song_hash}"
    return f"custom_level_{song.folder.name}"


def backup_player_data(player_dat_path: Path, raw: str) -> None:
    bak_dir = Path(__file__).parent.parent / "backups"
    bak_dir.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
    (bak_dir / f"PlayerData_{stamp}.dat.bak").write_text(raw, encoding="utf-8")
    # Build the sibling backup name by string concatenation rather than
    # `.with_suffix(".dat.bak")` — the multi-dot behaviour of `with_suffix`
    # shifted across Python 3.12+, so this avoids version-dependent output.
    sibling_bak = player_dat_path.parent / (player_dat_path.name + ".bak")
    sibling_bak.write_text(raw, encoding="utf-8")


def beat_saber_running() -> bool:
    """Best-effort check for a running Beat Saber process. Returns False if we can't tell."""
    if os.name != "nt":
        return False
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Beat Saber.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=creationflags,
        )
        return "Beat Saber.exe" in (result.stdout or "")
    except Exception:
        return False


def confirm_player_data_write(parent=None) -> bool:
    """Warn the user if Beat Saber is running before touching PlayerData.dat.

    Returns True if it's safe to proceed (game not detected, or user chose to continue).
    """
    if not beat_saber_running():
        return True
    try:
        return bool(messagebox.askyesno(
            "Beat Saber is running",
            "Beat Saber appears to be running. Saving now risks the game overwriting "
            "your change when it exits (or vice versa).\n\n"
            "Close Beat Saber first for safety.\n\nContinue anyway?",
            icon="warning",
            parent=parent,
        ))
    except Exception:
        # If we can't show the dialog for any reason, fall through and allow.
        return True


def _atomic_write_player_data(
    player_dat_path: Path,
    content: str,
    mtime_before: int,
    parent=None,
) -> bool:
    """Write content to player_dat_path atomically, aborting if mtime changed.

    ``mtime_before`` is an ``st_mtime_ns`` value — nanosecond resolution avoids
    the 1–2 s filesystem-granular false-equal from two writes in the same tick.

    Returns True on success. Shows an error and returns False otherwise.
    """
    try:
        if player_dat_path.stat().st_mtime_ns != mtime_before:
            messagebox.showerror(
                "PlayerData changed",
                "PlayerData.dat was modified by another process while we were updating it. "
                "Aborting to avoid overwriting changes. Please retry.",
                parent=parent,
            )
            return False
    except OSError:
        # If we can't stat, fall through and attempt the write.
        pass
    fd, tmp_str = tempfile.mkstemp(dir=str(player_dat_path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_str, player_dat_path)
    except Exception:
        Path(tmp_str).unlink(missing_ok=True)
        raise
    return True


def add_to_favorites(player_dat_path: Path, song: SongInfo, favorite_ids: set[str]) -> bool:
    """Add song to favorites in PlayerData.dat. Mutates favorite_ids. Returns True on success."""
    if not confirm_player_data_write():
        return False
    try:
        mtime_before = player_dat_path.stat().st_mtime_ns
        raw = player_dat_path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
        players = data.get("localPlayers", [])
        if not players:
            return False
        level_id = favorite_level_id(song)
        favs: list = players[0].setdefault("favoritesLevelIds", [])
        if level_id not in favs:
            favs.append(level_id)
        content = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if not _atomic_write_player_data(player_dat_path, content, mtime_before):
            return False
        # Write the backup only after the new content has been committed, so
        # aborted/no-op writes don't litter `backups/` and overwrite the
        # alongside-`.dat.bak` file.
        backup_player_data(player_dat_path, raw)
        favorite_ids.add(level_id)
        return True
    except Exception as exc:
        messagebox.showerror("Favorites Error", str(exc))
        return False


def remove_from_favorites(player_dat_path: Path, song: SongInfo, favorite_ids: set[str]) -> bool:
    """Remove song from favorites in PlayerData.dat. Mutates favorite_ids. Returns True on success."""
    if not confirm_player_data_write():
        return False
    try:
        mtime_before = player_dat_path.stat().st_mtime_ns
        raw = player_dat_path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
        players = data.get("localPlayers", [])
        if not players:
            return False
        to_remove = {f"custom_level_{song.folder.name}"}
        if song.song_hash:
            to_remove.add(f"custom_level_{song.song_hash}")
        favs: list = players[0].get("favoritesLevelIds", [])
        players[0]["favoritesLevelIds"] = [f for f in favs if f not in to_remove]
        content = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if not _atomic_write_player_data(player_dat_path, content, mtime_before):
            return False
        backup_player_data(player_dat_path, raw)
        favorite_ids -= to_remove
        return True
    except Exception as exc:
        messagebox.showerror("Favorites Error", str(exc))
        return False
