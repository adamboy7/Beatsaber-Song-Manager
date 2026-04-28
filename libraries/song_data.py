import re
import json
import datetime
from pathlib import Path


class SongInfo:
    __slots__ = (
        "folder", "song_id", "display_name",
        "song_name", "sub_name", "author",
        "mapper", "bpm", "cover_path", "audio_path", "created_at",
        "diff_labels", "song_hash",
    )

    def __init__(self, folder: Path):
        self.folder = folder
        self.song_id = ""
        self.display_name = folder.name
        self.song_name = ""
        self.sub_name = ""
        self.author = ""
        self.mapper = ""
        self.bpm = 0.0
        self.cover_path: Path | None = None
        self.audio_path: Path | None = None
        self.diff_labels: dict[int, str] = {}
        self.song_hash: str = ""
        # Use st_birthtime (Windows/macOS) with st_ctime as fallback
        stat = folder.stat()
        self.created_at: float = getattr(stat, "st_birthtime", stat.st_ctime)
        self._parse()

    def _parse(self):
        # Detect community format: "abcd1 (Song Name - Mapper)"
        m = re.match(r'^([A-Za-z0-9]+)\s+\((.+)\)$', self.folder.name)
        if m:
            self.song_id = m.group(1)

        # Read Info.dat (case-insensitive search)
        info_file = None
        for name in ("Info.dat", "info.dat", "INFO.DAT"):
            candidate = self.folder / name
            if candidate.exists():
                info_file = candidate
                break

        if info_file:
            try:
                data = json.loads(info_file.read_text(encoding="utf-8", errors="replace"))
                is_v4 = data.get("version", "").startswith("4")

                if is_v4:
                    song_obj = data.get("song", {})
                    audio_obj = data.get("audio", {})
                    self.song_name = song_obj.get("title", "")
                    self.sub_name  = song_obj.get("subTitle", "")
                    self.author    = song_obj.get("author", "")
                    self.bpm       = float(audio_obj.get("bpm", 0))
                    cover_filename = data.get("coverImageFilename", "")
                    audio_filename = audio_obj.get("songFilename", "")
                    mappers: list[str] = []
                    seen: set[str] = set()
                    for bm in data.get("difficultyBeatmaps", []):
                        for mapper_name in bm.get("beatmapAuthors", {}).get("mappers", []):
                            if mapper_name not in seen:
                                mappers.append(mapper_name)
                                seen.add(mapper_name)
                    self.mapper = ", ".join(mappers)
                else:
                    self.song_name = data.get("_songName", "")
                    self.sub_name  = data.get("_songSubName", "")
                    self.author    = data.get("_songAuthorName", "")
                    self.mapper    = data.get("_levelAuthorName", "")
                    self.bpm       = float(data.get("_beatsPerMinute", 0))
                    cover_filename = data.get("_coverImageFilename", "")
                    audio_filename = data.get("_songFilename", "")

                if cover_filename:
                    cp = self.folder / cover_filename
                    if cp.exists():
                        self.cover_path = cp

                if audio_filename:
                    ap = self.folder / audio_filename
                    if ap.exists():
                        self.audio_path = ap

                _DIFF_STR_TO_INT = {"Easy": 0, "Normal": 1, "Hard": 2, "Expert": 3, "ExpertPlus": 4}
                standard_labels: dict[int, str] = {}
                other_labels:    dict[int, str] = {}
                if is_v4:
                    for bm in data.get("difficultyBeatmaps", []):
                        char = bm.get("characteristic", "")
                        diff_int = _DIFF_STR_TO_INT.get(bm.get("difficulty", ""))
                        if diff_int is None:
                            continue
                        custom = bm.get("customData", {})
                        label = custom.get("difficultyLabel", "").strip()
                        if not label:
                            continue
                        if char == "Standard":
                            standard_labels[diff_int] = label
                        else:
                            other_labels.setdefault(diff_int, label)
                else:
                    for bms in data.get("_difficultyBeatmapSets", []):
                        char = bms.get("_beatmapCharacteristicName", "")
                        for bm in bms.get("_difficultyBeatmaps", []):
                            diff_int = _DIFF_STR_TO_INT.get(bm.get("_difficulty", ""))
                            if diff_int is None:
                                continue
                            custom = bm.get("_customData", bm.get("customData", {}))
                            label = custom.get("_difficultyLabel", custom.get("difficultyLabel", "")).strip()
                            if not label:
                                continue
                            if char == "Standard":
                                standard_labels[diff_int] = label
                            else:
                                other_labels.setdefault(diff_int, label)
                self.diff_labels = {**other_labels, **standard_labels}
            except Exception:
                pass

        # Build display name
        if self.song_name:
            self.display_name = self.song_name
            if self.sub_name:
                self.display_name += f" {self.sub_name}"
        else:
            self.display_name = self.folder.name

    @property
    def bpm_str(self) -> str:
        return f"{self.bpm:.0f} BPM" if self.bpm else ""

    @property
    def author_line(self) -> str:
        parts = []
        if self.author:
            parts.append(self.author)
        if self.mapper:
            parts.append(f"mapped by {self.mapper}")
        return "  •  ".join(parts)


def load_song_hashes(custom_levels: Path) -> dict[str, str]:
    """Return {folder_name: songHash} by reading SongCore's SongHashData.dat."""
    hash_file = custom_levels.parent.parent / "UserData" / "SongCore" / "SongHashData.dat"
    result: dict[str, str] = {}
    try:
        data = json.loads(hash_file.read_text(encoding="utf-8", errors="replace"))
        for key, val in data.items():
            folder_name = Path(key.replace("\\", "/")).name
            song_hash = val.get("songHash", "")
            if folder_name and song_hash:
                result[folder_name] = song_hash.upper()
    except Exception:
        pass
    return result


def load_songs(custom_levels: Path) -> list[SongInfo]:
    songs = []
    for entry in custom_levels.iterdir():
        if entry.is_dir():
            songs.append(SongInfo(entry))
    # Newest folder first
    songs.sort(key=lambda s: s.created_at, reverse=True)
    return songs
