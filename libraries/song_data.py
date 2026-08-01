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


def _cinema_safe_filename(title: str) -> str:
    """Replicate Cinema's illegal-filesystem-char replacement so we derive the
    same video filename the mod would (e.g. "DECO*27 … feat. Miku" becomes
    "DECO_27 … feat_ Miku"). Cinema replaces invalid filename chars *and*
    periods with underscores."""
    return re.sub(r'[<>:"/\\|?*.\x00-\x1f]', "_", title)


def _shorten_filename(folder: Path, name: str) -> str:
    """Cinema's ``Util.ShortenFilename``: keep the full path under MAX_PATH.

    The mod budgets 259 characters minus the level directory, ".mp4" and the
    ".fXXXX" yt-dlp adds to format-specific parts, then truncates the name to
    fit — so a very long title produces a *different* file on disk than the
    untruncated one, and we have to truncate identically or we'd look for a
    video the mod never wrote (and write one it will never find).
    """
    allowed = 259 - len(str(folder)) - len(".mp4") - len(".fxxxx")
    if allowed >= len(name):
        return name
    return name[:allowed] if allowed > 0 else name


def _cinema_video_filename(folder: Path, raw: str) -> str:
    """Resolve a manifest's explicit ``videoFile`` to one legal filename.

    Cinema's ``VideoConfig.GetVideoFileName`` trusts ``videoFile`` verbatim and
    only sanitises the name when it derives one from the title. That is fine
    until a mapper pastes the raw YouTube title in as the filename — the title
    of 22e58 ("… 禁止令 / 草薙寧々 …") carries a slash, so the mod hands yt-dlp a
    *nested* output path, yt-dlp rewrites the folder component to be legal (a
    trailing space becomes "#"), and the video lands in a subfolder that
    nothing afterwards looks in. In-game the map is stuck at "not downloaded"
    forever; here it downloaded and then vanished from the visualizer.

    The mod has no handling for it, so this is the one place we deliberately
    diverge: run the mapper's value through the same replacement Cinema uses
    for titles (minus the period rule, which would eat a legitimate extension)
    to get a flat filename. For this map that lands on exactly the name Cinema
    would have picked had the mapper left ``videoFile`` out.
    """
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", raw).strip()
    name = name.rstrip(" .")
    name = _shorten_filename(folder, name)
    if not name.endswith(".mp4"):
        name += ".mp4"
    return name


def _loose_name(name: str) -> str:
    """Compare path components the way the filesystem/yt-dlp mangle them."""
    return re.sub(r"[\s.#]+$", "", name.strip()).casefold()


def _misplaced_video(folder: Path, raw: str) -> Path | None:
    """Find a video an earlier download dropped into a subfolder.

    Only reachable when ``videoFile`` contained a path separator (see
    ``_cinema_video_filename``). Components are matched loosely because the
    write path mangles them: yt-dlp turns a component's trailing whitespace
    into "#", and Windows trims trailing spaces and dots outright. Returning
    the stray file means an affected song plays immediately instead of the
    user paying for the same download twice.
    """
    parts = [p for p in re.split(r"[\\/]+", raw) if p.strip()]
    if len(parts) < 2:
        return None
    current = folder
    for part in parts[:-1]:
        current = _matching_child(current, part, want_dir=True)
        if current is None:
            return None
    return _matching_child(current, parts[-1], want_dir=False)


def _matching_child(parent: Path, name: str, want_dir: bool) -> Path | None:
    wanted = _loose_name(name)
    try:
        entries = list(parent.iterdir())
    except OSError:
        return None
    for entry in entries:
        try:
            if entry.is_dir() != want_dir:
                continue
        except OSError:
            continue
        if _loose_name(entry.name) == wanted:
            return entry
    return None


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
        "cinema_video_id", "cinema_video_file", "cinema_video_file_raw",
        "search_blob",
    )

    def __init__(self, folder: Path, info_path: Path | None = None):
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
        # YouTube video ID and the filename Cinema will save the video under
        # (explicit videoFile, or derived from the title like Cinema does).
        self.cinema_video_id: str = ""
        self.cinema_video_file: str = ""
        self.cinema_video_file_raw: str = ""
        self.search_blob: str = ""
        # Use st_birthtime (Windows/macOS) with st_ctime as fallback.
        # A racy filesystem (folder deleted mid-scan, permission denied)
        # shouldn't take down the whole load_songs() call.
        try:
            stat = folder.stat()
            self.created_at: float = getattr(stat, "st_birthtime", stat.st_ctime)
        except OSError:
            self.created_at = 0.0
        self._parse(info_path)

    def _parse(self, info_path: Path | None = None):
        # Detect community format: "abcd1 (Song Name - Mapper)"
        m = re.match(r'^([A-Za-z0-9]+)\s+\((.+)\)$', self.folder.name)
        if m:
            self.song_id = m.group(1)

        # Read Info.dat (case-insensitive search unless the caller already
        # resolved the path, e.g. load_songs() via _find_info_dat).
        info_file = info_path if info_path is not None else _find_info_dat(self.folder)

        if info_file:
            try:
                data = json.loads(info_file.read_text(encoding="utf-8", errors="replace"))
                is_v4 = str(data.get("version", "")).startswith("4")

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

        self.update_search_blob()

    def update_search_blob(self) -> None:
        """Recompute the cached lowercased search text; call after editing
        display_name/author/mapper/song_id outside of _parse()."""
        self.search_blob = "\n".join((
            self.display_name.lower(), self.author.lower(),
            self.mapper.lower(), self.song_id.lower(),
        ))

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
            self.cinema_video_id = str(data.get("videoID", "") or "")
            raw_filename = str(data.get("videoFile", "") or "")
            self.cinema_video_file_raw = raw_filename
            if raw_filename:
                video_filename = _cinema_video_filename(self.folder, raw_filename)
            else:
                # Cinema derives the filename from the title when videoFile
                # is absent from the manifest.
                video_filename = ""
                title = str(data.get("title", "") or "").strip()
                if title:
                    video_filename = _shorten_filename(
                        self.folder, _cinema_safe_filename(title)
                    ) + ".mp4"
                elif self.cinema_video_id:
                    video_filename = self.cinema_video_id + ".mp4"
            if video_filename:
                self.cinema_video_file = video_filename
                self.cinema_video_path = None
                vp = self.folder / video_filename
                if vp.exists():
                    self.cinema_video_path = vp
                elif raw_filename:
                    self.cinema_video_path = _misplaced_video(
                        self.folder, raw_filename
                    )
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
    def has_illegal_cinema_filename(self) -> bool:
        """True when the manifest's videoFile can't exist as written.

        Cinema stores the name verbatim and then can never find the file it
        downloaded, so the map is stuck at "not downloaded" in-game. We can
        still play it, but only a manifest rewrite fixes the mod — hence the
        offer before downloading.
        """
        return bool(
            self.cinema_video_file_raw
            and self.cinema_video_file
            and self.cinema_video_file_raw != self.cinema_video_file
        )

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
        if not diff_path.exists():
            # A referenced diff that's missing (partial download, manual
            # deletion) means our hash would differ from SongCore's — bail
            # rather than produce a wrong hash, same as the unreadable case.
            print(
                f"compute_song_hash: skipping {song_folder.name}: "
                f"missing referenced file {fn}"
            )
            return ""
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


def _folder_fingerprint(folder: Path) -> str:
    """Fingerprint of the files directly in the song folder, for the hash cache.

    A digest over every file's (name, mtime_ns, size), sorted. The previous
    key — the *max* mtime across the folder — went stale on deletions and
    renames: removing or renaming a referenced difficulty file changes no
    surviving file's mtime, so the cache kept serving a hash for a folder
    ``compute_song_hash`` would now reject. Including names and sizes makes
    additions, deletions, renames and edits all invalidate. Returns "" when
    the folder can't be listed (callers treat that as uncacheable).
    """
    entries: list[str] = []
    try:
        for child in folder.iterdir():
            try:
                if not child.is_file():
                    continue
                stat = child.stat()
            except OSError:
                continue
            entries.append(f"{child.name}\n{stat.st_mtime_ns}\n{stat.st_size}")
    except OSError:
        return ""
    entries.sort()
    digest = hashlib.sha1("\0".join(entries).encode("utf-8", "surrogatepass"))
    return digest.hexdigest()


def load_song_hashes(custom_levels: Path) -> dict[str, str]:
    """Return {folder_name: songHash}.

    Primary source is SongCore's SongHashData.dat. For any custom-level
    folder that file doesn't cover (e.g. songs added since SongCore last
    ran), we fall back to computing the hash from the map files directly.

    The fallback computation is cached in the app-data folder's
    ``.bsm_hash_cache.json`` (see app_config.hash_cache_path) keyed by folder
    name + a fingerprint of the folder's files (see ``_folder_fingerprint``)
    so subsequent launches with the same library skip the expensive SHA1 work
    for unchanged folders.
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

    # Load the sidecar cache (best-effort). Stored in the app-data folder so it
    # survives a CustomLevels folder change and never needs write access to a
    # (possibly read-only) game directory.
    from libraries import app_config
    cache_path = app_config.hash_cache_path()
    cache: dict[str, dict] = {}
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(cache, dict):
            cache = {}
    except Exception:
        cache = {}

    new_cache: dict[str, dict] = {}
    dirty = False

    # Fallback: compute hashes for folders SongCore didn't list, and force a
    # recompute for any folder mid-edit (an Info.dat.bak means the in-game
    # editor may have rewritten Info.dat since SongCore last hashed it).
    try:
        for entry in custom_levels.iterdir():
            if not entry.is_dir():
                continue
            has_edit_bak = any(
                (entry / bak_name).exists()
                for bak_name in ("Info.dat.bak", "info.dat.bak", "INFO.DAT.bak")
            )
            if has_edit_bak:
                fp = _folder_fingerprint(entry)
                cached = cache.get(entry.name)
                if (
                    isinstance(cached, dict)
                    and fp
                    and cached.get("fp") == fp
                    and isinstance(cached.get("hash"), str)
                    and cached["hash"]
                ):
                    result[entry.name] = cached["hash"]
                    new_cache[entry.name] = cached
                    continue
                recomputed = compute_song_hash(entry)
                if recomputed:
                    result[entry.name] = recomputed
                    new_cache[entry.name] = {"fp": fp, "hash": recomputed}
                    dirty = True
                continue
            if entry.name in result:
                continue
            fp = _folder_fingerprint(entry)
            cached = cache.get(entry.name)
            if (
                isinstance(cached, dict)
                and fp
                and cached.get("fp") == fp
                and isinstance(cached.get("hash"), str)
                and cached["hash"]
            ):
                result[entry.name] = cached["hash"]
                new_cache[entry.name] = cached
                continue
            computed = compute_song_hash(entry)
            if computed:
                result[entry.name] = computed
                new_cache[entry.name] = {"fp": fp, "hash": computed}
                dirty = True
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


def _find_info_dat(folder: Path) -> Path | None:
    """Return the folder's Info.dat path (case-insensitive), or None."""
    for name in ("Info.dat", "info.dat", "INFO.DAT"):
        candidate = folder / name
        if candidate.exists():
            return candidate
    return None


def load_songs(custom_levels: Path) -> list[SongInfo]:
    songs = []
    for entry in custom_levels.iterdir():
        if not entry.is_dir():
            continue
        info_path = _find_info_dat(entry)
        if info_path is not None:
            songs.append(SongInfo(entry, info_path))
    # Newest folder first
    songs.sort(key=lambda s: s.created_at, reverse=True)
    return songs
