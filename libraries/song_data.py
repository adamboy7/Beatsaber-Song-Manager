import re
import json
import hashlib
import datetime
from pathlib import Path


_TAGS_FILE = "tags.json"


def _load_custom_tags(folder: Path) -> frozenset[str]:
    try:
        data = json.loads((folder / _TAGS_FILE).read_text(encoding="utf-8"))
        return frozenset(str(t) for t in data.get("tags", []))
    except Exception:
        return frozenset()


def save_custom_tags(folder: Path, tags: frozenset | set) -> None:
    (folder / _TAGS_FILE).write_text(
        json.dumps({"tags": sorted(tags)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class SongInfo:
    __slots__ = (
        "folder", "song_id", "display_name",
        "song_name", "sub_name", "author",
        "mapper", "bpm", "cover_path", "audio_path", "created_at",
        "diff_labels", "difficulties", "song_hash", "custom_tags",
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
        self.difficulties: set[int] = set()
        self.song_hash: str = ""
        self.custom_tags: frozenset[str] = frozenset()
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
                        self.difficulties.add(diff_int)
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
                            self.difficulties.add(diff_int)
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

        self.custom_tags = _load_custom_tags(self.folder)

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


def compute_song_hash(song_folder: Path) -> str:
    """Compute the Beat Saber song hash for a folder the way SongCore does:
    SHA1 over Info.dat's bytes followed by the bytes of every difficulty .dat
    file referenced in Info.dat (in the order they appear). Returns an
    uppercase hex digest, or "" if the folder isn't a valid map.

    This is the fallback we use when SongCore hasn't yet indexed a song
    (e.g. it was just installed and Beat Saber hasn't been relaunched since).
    """
    # Find Info.dat (case-insensitive)
    info_file = None
    for name in ("Info.dat", "info.dat", "INFO.DAT"):
        candidate = song_folder / name
        if candidate.exists():
            info_file = candidate
            break
    if info_file is None:
        return ""

    try:
        info_bytes = info_file.read_bytes()
        data = json.loads(info_bytes.decode("utf-8", errors="replace"))
    except Exception:
        return ""

    # Gather difficulty filenames in the order they appear in Info.dat.
    # v4 maps use a flat "difficultyBeatmaps" list; v2/v3 nest them under
    # "_difficultyBeatmapSets" -> "_difficultyBeatmaps".
    diff_filenames: list[str] = []
    is_v4 = str(data.get("version", "")).startswith("4")
    if is_v4:
        for bm in data.get("difficultyBeatmaps", []):
            fn = bm.get("beatmapDataFilename") or bm.get("lightshowDataFilename")
            if fn:
                diff_filenames.append(fn)
    else:
        for bms in data.get("_difficultyBeatmapSets", []):
            for bm in bms.get("_difficultyBeatmaps", []):
                fn = bm.get("_beatmapFilename")
                if fn:
                    diff_filenames.append(fn)

    sha = hashlib.sha1()
    sha.update(info_bytes)
    for fn in diff_filenames:
        diff_path = song_folder / fn
        if diff_path.exists():
            try:
                sha.update(diff_path.read_bytes())
            except Exception:
                # If a referenced diff can't be read, the hash will differ from
                # SongCore's — better to bail than to produce a wrong hash.
                return ""
    return sha.hexdigest().upper()


def load_song_hashes(custom_levels: Path) -> dict[str, str]:
    """Return {folder_name: songHash}.

    Primary source is SongCore's SongHashData.dat. For any custom-level
    folder that file doesn't cover (e.g. songs added since SongCore last
    ran), we fall back to computing the hash from the map files directly.
    """
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

    # Fallback: compute hashes for folders SongCore didn't list.
    try:
        for entry in custom_levels.iterdir():
            if not entry.is_dir():
                continue
            if entry.name in result:
                continue
            computed = compute_song_hash(entry)
            if computed:
                result[entry.name] = computed
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
