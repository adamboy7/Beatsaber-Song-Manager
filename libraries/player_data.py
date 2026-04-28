import os
import json
from pathlib import Path

from libraries.song_data import SongInfo

# Beat Saber difficulty int → label
DIFF_LABELS = {0: "Easy", 1: "Normal", 2: "Hard", 3: "Expert", 4: "ExpertPlus"}

# Beat Saber maxRank int → display string
RANK_LABELS = {0: "E", 1: "D", 2: "C", 3: "B", 4: "A", 5: "S", 6: "SS"}

# Colour per rank for display
RANK_COLORS = {
    "SS": "#FFD700", "S": "#FFD700",
    "A":  "#84e060", "B": "#60b4e0",
    "C":  "#e0c060", "D": "#e08060",
    "E":  "#888888", "DNF": "#555555",
}


def find_player_data() -> tuple["Path | None", str]:
    """Locate PlayerData.dat. Returns (path_or_None, debug_string)."""
    debug = []
    bs_relative = Path("Hyperbolic Magnetism") / "Beat Saber" / "PlayerData.dat"

    # Strategy 1: Registry — 'Local AppData' value tells us the real path;
    # LocalLow is always a sibling folder (Local -> LocalLow).
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
        )
        local_appdata, _ = winreg.QueryValueEx(key, "Local AppData")
        winreg.CloseKey(key)
        local_low = Path(local_appdata).parent / "LocalLow"
        debug.append(f"Registry->LocalLow:{local_low}")
        candidate = local_low / bs_relative
        if candidate.exists():
            return candidate, " | ".join(debug)
    except Exception as e:
        debug.append(f"registry_fail:{e}")

    # Strategy 2: USERPROFILE environment variable
    try:
        profile = os.environ.get("USERPROFILE", "")
        if profile:
            local_low = Path(profile) / "AppData" / "LocalLow"
            debug.append(f"USERPROFILE->LocalLow:{local_low}")
            candidate = local_low / bs_relative
            if candidate.exists():
                return candidate, " | ".join(debug)
    except Exception as e:
        debug.append(f"userprofile_fail:{e}")

    # Strategy 3: Path.home()
    local_low = Path.home() / "AppData" / "LocalLow"
    debug.append(f"home()->LocalLow:{local_low}")
    candidate = local_low / bs_relative
    if candidate.exists():
        return candidate, " | ".join(debug)

    return None, "NOT_FOUND | " + " | ".join(debug)


class DiffStat:
    """High score, rank string, play count, and FC flag for one difficulty."""
    __slots__ = ("score", "rank", "plays", "full_combo")

    def __init__(self, score: int, rank: str, plays: int, full_combo: bool = False):
        self.score      = score
        self.rank       = rank
        self.plays      = plays
        self.full_combo = full_combo


def load_favorites(player_data_path: Path) -> set[str]:
    """Return the set of favorited levelIds from PlayerData.dat."""
    try:
        raw = json.loads(player_data_path.read_text(encoding="utf-8", errors="replace"))
        players = raw.get("localPlayers", [])
        if not players:
            return set()
        return set(players[0].get("favoritesLevelIds", []))
    except Exception:
        return set()


def load_player_stats(player_data_path: Path) -> dict[str, dict[int, DiffStat]]:
    """Return {level_id: {difficulty_int: DiffStat}} for all entries."""
    stats: dict[str, dict[int, DiffStat]] = {}
    try:
        raw = json.loads(player_data_path.read_text(encoding="utf-8", errors="replace"))
        players = raw.get("localPlayers", [])
        if not players:
            return stats
        entries = players[0].get("levelsStatsData", [])
        for entry in entries:
            plays = entry.get("playCount", 0)
            if plays == 0:
                continue  # Beat Saber creates zero entries on scan; not actual play data

            level_id   = entry.get("levelId", "")
            diff       = entry.get("difficulty", 0)
            score      = entry.get("highScore", 0)
            rank_int   = entry.get("maxRank", 0)
            full_combo = bool(entry.get("fullCombo", False))

            rank_str = RANK_LABELS.get(rank_int, "E") if score > 0 else "DNF"

            stat = DiffStat(score, rank_str, plays, full_combo)
            stats.setdefault(level_id, {})[diff] = stat
    except Exception:
        pass
    return stats


def song_level_ids(song: SongInfo) -> list[str]:
    """Generate the candidate levelId strings Beat Saber uses for this song.

    Beat Saber stores custom maps as:
      • "custom_level_{folder_name}"  — for community maps (short hex or SHA1)
      • levelId == folder name        — for built-in / OST maps
    """
    ids = []
    if song.song_hash:
        ids.append(f"custom_level_{song.song_hash}")   # most accurate: matches PlayerData
    ids.append(f"custom_level_{song.folder.name}")     # fallback: old folder-name form
    ids.append(song.folder.name)                       # fallback for OST / built-in
    return ids


def get_song_stats(song: SongInfo,
                   all_stats: dict[str, dict[int, DiffStat]]
                   ) -> dict[int, DiffStat] | None:
    """Merge stats across all known levelId forms for this song.

    Vanilla Beat Saber writes custom_level_{folder_name}; SongCore writes
    custom_level_{hash}. Both may exist if the player switched between them,
    so we collect all matches and combine them per-difficulty.
    """
    merged: dict[int, DiffStat] = {}
    for lid in song_level_ids(song):
        entry = all_stats.get(lid)
        if not entry:
            continue
        for diff_int, stat in entry.items():
            if diff_int not in merged:
                merged[diff_int] = DiffStat(stat.score, stat.rank, stat.plays, stat.full_combo)
            else:
                existing = merged[diff_int]
                fc = existing.full_combo or stat.full_combo
                if stat.score > existing.score:
                    merged[diff_int] = DiffStat(stat.score, stat.rank, existing.plays + stat.plays, fc)
                else:
                    merged[diff_int] = DiffStat(existing.score, existing.rank, existing.plays + stat.plays, fc)
    return merged if merged else None


def format_diff_stats(diff_stats: dict[int, DiffStat],
                      custom_labels: dict[int, str] | None = None,
                      ) -> tuple[list[tuple[str, bool]], str]:
    """Return ([(text, is_fc), ...], plays_line) for display in the row."""
    ordered = sorted(diff_stats.items())
    parts: list[tuple[str, bool]] = []
    total_plays = 0
    for diff_int, stat in ordered:
        label = (custom_labels or {}).get(diff_int) or DIFF_LABELS.get(diff_int, f"D{diff_int}")
        short = {"Easy": "Easy", "Normal": "Norm", "Hard": "Hard",
                 "Expert": "Expert", "ExpertPlus": "E+"}.get(label, label)
        score_str = f"{stat.score:,}" if stat.score else "0"
        parts.append((f"{short}:{score_str} | {stat.rank}", stat.full_combo))
        total_plays += stat.plays

    plays_line = f"Plays: {total_plays}" if total_plays else ""
    return parts, plays_line
