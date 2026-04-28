import json
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
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    (bak_dir / f"PlayerData_{stamp}.dat.bak").write_text(raw, encoding="utf-8")
    player_dat_path.with_suffix(".dat.bak").write_text(raw, encoding="utf-8")


def add_to_favorites(player_dat_path: Path, song: SongInfo, favorite_ids: set[str]) -> bool:
    """Add song to favorites in PlayerData.dat. Mutates favorite_ids. Returns True on success."""
    try:
        raw = player_dat_path.read_text(encoding="utf-8", errors="replace")
        backup_player_data(player_dat_path, raw)
        data = json.loads(raw)
        players = data.get("localPlayers", [])
        if not players:
            return False
        level_id = favorite_level_id(song)
        favs: list = players[0].setdefault("favoritesLevelIds", [])
        if level_id not in favs:
            favs.append(level_id)
        player_dat_path.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        favorite_ids.add(level_id)
        return True
    except Exception as exc:
        messagebox.showerror("Favorites Error", str(exc))
        return False


def remove_from_favorites(player_dat_path: Path, song: SongInfo, favorite_ids: set[str]) -> bool:
    """Remove song from favorites in PlayerData.dat. Mutates favorite_ids. Returns True on success."""
    try:
        raw = player_dat_path.read_text(encoding="utf-8", errors="replace")
        backup_player_data(player_dat_path, raw)
        data = json.loads(raw)
        players = data.get("localPlayers", [])
        if not players:
            return False
        to_remove = {f"custom_level_{song.folder.name}"}
        if song.song_hash:
            to_remove.add(f"custom_level_{song.song_hash}")
        favs: list = players[0].get("favoritesLevelIds", [])
        players[0]["favoritesLevelIds"] = [f for f in favs if f not in to_remove]
        player_dat_path.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        favorite_ids -= to_remove
        return True
    except Exception as exc:
        messagebox.showerror("Favorites Error", str(exc))
        return False
