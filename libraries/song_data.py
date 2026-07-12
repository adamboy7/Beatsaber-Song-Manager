import re
import json
import hashlib
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


def _has_cinema_video(folder: Path) -> bool:
    """True if a Cinema mod video manifest exists in the folder (case-insensitive)."""
    for name in ("cinema-video.json", "Cinema-Video.json", "CINEMA-VIDEO.JSON"):
        if (folder / name).exists():
            return True
    return False


class SongInfo:
    __slots__ = (
        "folder", "song_id", "display_name",
        "song_name", "sub_name", "author",
        "mapper", "bpm", "cover_path", "audio_path", "created_at",
        "diff_labels", "difficulties", "song_hash", "custom_tags",
        "mod_required", "mod_suggested", "has_cinema_video",
        "cinema_video_path", "cinema_video_offset_ms", "cinema_video_duration_s",
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
        self.mod_required: frozenset[str] = frozenset()
        self.mod_suggested: frozenset[str] = frozenset()
        self.has_cinema_video: bool = False
        # Set only when a Cinema-configured video is actually present on disk
        # (the manifest can reference a video the user hasn't downloaded yet).
        self.cinema_video_path: Path | None = None
        self.cinema_video_offset_ms: int = 0
        self.cinema_video_duration_s: int = 0
        # Use st_birthtime (Windows/macOS) with st_ctime as fallback.
        # A racy filesystem (folder deleted mid-scan, permission denied)
        # shouldn't take down the whole load_songs() call.
        try:
            stat = folder.stat()
            self.created_at: float = getattr(stat, "st_birthtime", stat.st_ctime)
        except OSError:
            self.created_at = 0.0
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
                required: set[str] = set()
                suggested: set[str] = set()

                def _collect_mods(custom: dict) -> None:
                    for name in custom.get("_requirements", custom.get("requirements", [])) or []:
                        name = str(name).strip()
                        if name and name.lower() != "none":
                            required.add(name)
                    for name in custom.get("_suggestions", custom.get("suggestions", [])) or []:
                        name = str(name).strip()
                        if name and name.lower() != "none":
                            suggested.add(name)

                if is_v4:
                    for bm in data.get("difficultyBeatmaps", []):
                        char = bm.get("characteristic", "")
                        diff_int = _DIFF_STR_TO_INT.get(bm.get("difficulty", ""))
                        if diff_int is None:
                            continue
                        self.difficulties.add(diff_int)
                        custom = bm.get("customData", {})
                        _collect_mods(custom)
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
                            _collect_mods(custom)
                            label = custom.get("_difficultyLabel", custom.get("difficultyLabel", "")).strip()
                            if not label:
                                continue
                            if char == "Standard":
                                standard_labels[diff_int] = label
                            else:
                                other_labels.setdefault(diff_int, label)
                self.diff_labels = {**other_labels, **standard_labels}
                self.mod_required = frozenset(required)
                self.mod_suggested = frozenset(suggested)
            except Exception:
                pass

        self.has_cinema_video = _has_cinema_video(self.folder)
        if self.has_cinema_video:
            self._parse_cinema_video()
        self.custom_tags = _load_custom_tags(self.folder)

        # Build display name
        if self.song_name:
            self.display_name = self.song_name
            if self.sub_name:
                self.display_name += f" {self.sub_name}"
        else:
            self.display_name = self.folder.name

    def _parse_cinema_video(self) -> None:
        """Read cinema-video.json for the video filename / offset / duration.

        cinema_video_path is only set if the referenced video file actually
        exists in the song folder — the manifest can reference a video the
        user hasn't downloaded in-game yet.
        """
        for name in ("cinema-video.json", "Cinema-Video.json", "CINEMA-VIDEO.JSON"):
            candidate = self.folder / name
            if not candidate.exists():
                continue
            try:
                data = json.loads(candidate.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                return
            video_filename = data.get("videoFile", "")
            if video_filename:
                vp = self.folder / video_filename
                if vp.exists():
                    self.cinema_video_path = vp
            try:
                self.cinema_video_offset_ms = int(data.get("offset", 0) or 0)
            except (TypeError, ValueError):
                self.cinema_video_offset_ms = 0
            try:
                self.cinema_video_duration_s = int(data.get("duration", 0) or 0)
            except (TypeError, ValueError):
                self.cinema_video_duration_s = 0
            return

    @property
    def has_playable_cinema_video(self) -> bool:
        """True when a Cinema video is configured *and* the file is downloaded."""
        return self.cinema_video_path is not None

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

    @property
    def requires_chroma(self) -> bool:
        return any("chroma" in m.lower() for m in self.mod_required)

    @property
    def requires_noodle(self) -> bool:
        return any("noodle" in m.lower() for m in self.mod_required)

    @property
    def requires_mapping_extensions(self) -> bool:
        return any("mapping extensions" in m.lower() for m in self.mod_required)

    @property
    def has_cinema(self) -> bool:
        """True when Cinema is recommended/required or a cinema-video.json is present.

        A map would essentially never *require* Cinema (it's a purely visual
        mod), but it's checked here too for completeness.
        """
        if self.has_cinema_video:
            return True
        return any("cinema" in m.lower() for m in self.mod_suggested) or \
            any("cinema" in m.lower() for m in self.mod_required)


def compute_song_hash(song_folder: Path, info_file: Path | None = None) -> str:
    """Compute the Beat Saber song hash for a folder the way SongCore does:
    SHA1 over Info.dat's bytes followed by the bytes of every difficulty .dat
    file referenced in Info.dat (in the order they appear). Returns an
    uppercase hex digest, or "" if the folder isn't a valid map.

    Pass info_file to override which file is used as Info.dat (e.g. a .bak).

    This is the fallback we use when SongCore hasn't yet indexed a song
    (e.g. it was just installed and Beat Saber hasn't been relaunched since).
    """
    if info_file is None:
        # Find Info.dat (case-insensitive)
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
            # SongCore hashes both the beatmap file and the lightshow file
            # when present, in the order they appear on the entry.
            beatmap_fn = bm.get("beatmapDataFilename")
            if beatmap_fn:
                diff_filenames.append(beatmap_fn)
            lightshow_fn = bm.get("lightshowDataFilename")
            if lightshow_fn:
                diff_filenames.append(lightshow_fn)
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
            except Exception as exc:
                # If a referenced diff can't be read, the hash will differ from
                # SongCore's — better to bail than to produce a wrong hash.
                # Log so the silent dedupe/playlist-missing failure is at least
                # diagnosable from stdout.
                print(
                    f"compute_song_hash: skipping {song_folder.name}: "
                    f"could not read {fn}: {exc}"
                )
                return ""
    return sha.hexdigest().upper()


def _folder_mtime(folder: Path) -> float:
    """Return the latest mtime of Info.dat (case-insensitive) or 0.0 if missing."""
    for name in ("Info.dat", "info.dat", "INFO.DAT"):
        candidate = folder / name
        if candidate.exists():
            try:
                return candidate.stat().st_mtime
            except OSError:
                return 0.0
    return 0.0


def load_song_hashes(custom_levels: Path) -> dict[str, str]:
    """Return {folder_name: songHash}.

    Primary source is SongCore's SongHashData.dat. For any custom-level
    folder that file doesn't cover (e.g. songs added since SongCore last
    ran), we fall back to computing the hash from the map files directly.

    The fallback computation is cached in ``<custom_levels>/.bsm_hash_cache.json``
    keyed by folder name + Info.dat mtime so subsequent launches with the same
    library skip the expensive SHA1 work for unchanged folders.
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

    # Load the sidecar cache (best-effort).
    cache_path = custom_levels / ".bsm_hash_cache.json"
    cache: dict[str, dict] = {}
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(cache, dict):
            cache = {}
    except Exception:
        cache = {}

    new_cache: dict[str, dict] = {}
    dirty = False

    # Fallback: compute hashes for folders SongCore didn't list.
    try:
        for entry in custom_levels.iterdir():
            if not entry.is_dir():
                continue
            if entry.name in result:
                continue
            mtime = _folder_mtime(entry)
            cached = cache.get(entry.name)
            if (
                isinstance(cached, dict)
                and cached.get("mtime") == mtime
                and isinstance(cached.get("hash"), str)
                and cached["hash"]
            ):
                result[entry.name] = cached["hash"]
                new_cache[entry.name] = cached
                continue
            computed = compute_song_hash(entry)
            if computed:
                result[entry.name] = computed
                new_cache[entry.name] = {"mtime": mtime, "hash": computed}
                dirty = True
    except Exception:
        pass

    try:
        for entry in custom_levels.iterdir():
            if not entry.is_dir():
                continue
            has_edit_bak = any(
                (entry / bak_name).exists()
                for bak_name in ("Info.dat.bak", "info.dat.bak", "INFO.DAT.bak")
            )
            if not has_edit_bak:
                continue
            recomputed = compute_song_hash(entry)
            if recomputed:
                result[entry.name] = recomputed
    except Exception:
        pass

    # Drop stale entries (folder gone) and persist if we touched anything.
    if dirty or len(new_cache) != len(cache):
        try:
            cache_path.write_text(
                json.dumps(new_cache, separators=(",", ":")),
                encoding="utf-8",
            )
        except Exception:
            pass

    return result


def _has_info_dat(folder: Path) -> bool:
    """True if the folder contains an Info.dat (case-insensitive)."""
    return any(
        (folder / name).exists()
        for name in ("Info.dat", "info.dat", "INFO.DAT")
    )


def load_songs(custom_levels: Path) -> list[SongInfo]:
    songs = []
    for entry in custom_levels.iterdir():
        if entry.is_dir() and _has_info_dat(entry):
            songs.append(SongInfo(entry))
    # Newest folder first
    songs.sort(key=lambda s: s.created_at, reverse=True)
    return songs
