"""Read/write access to a song folder's ``cinema-video.json``.

``song_data`` parses this manifest for display (video id, filename, offset,
duration); this module is the only place that *writes* it — for the offset
editor, and for creating a config from scratch out of a YouTube link. The
pure helpers it needs to do that (URL parsing, Cinema's filename derivation)
live here too, so the reader and the writer share one implementation.

Two behaviours here are copied deliberately from the Cinema mod itself rather
than invented. Both were read out of its source, at
https://github.com/Kevga/BeatSaberCinema — paths below are relative to that
repository:

* **Offset sign.** ``BeatSaberCinema/VideoMenu/Views/video-menu.bsml`` labels
  its ``+20 ms`` button "Starts video earlier", and
  ``BeatSaberCinema/Screen/PlaybackController.cs`` computes
  ``(time * speed) + (offset / 1000f)``. A positive offset therefore means
  the video runs *ahead* of the song, matching
  ``VisualizerWindow._video_pos``.
* **``userSettings``.** ``VideoMenu.CustomizeOffset``
  (``BeatSaberCinema/VideoMenu/VideoMenu.cs``) records
  ``customOffset``/``originalOffset`` before letting the user move a
  mapper-provided offset (``configByMapper: true``), so the mod can tell an
  overridden config from a stock one and offer to reset it. We do the same,
  otherwise an edit made here looks to Cinema like the mapper's own value.

* **Video filename derivation.** ``VideoConfig.GetVideoFileName`` +
  ``Util.FilterEmoji`` / ``ReplaceIllegalFilesystemChars`` / ``ShortenFilename``
  decide where the mod expects the ``.mp4`` to sit. ``derive_video_filename``
  below reproduces that chain exactly; a single character of divergence means
  the mod re-downloads a video we already fetched.

Unknown keys are round-tripped untouched: Cinema writes far more fields than
we parse (screen placement, colour correction, environment modifications),
and dropping them would silently break the map.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from libraries.fs_utils import atomic_write_text

# Same spellings song_data probes for, in the same order.
CONFIG_NAMES = ("cinema-video.json", "Cinema-Video.json", "CINEMA-VIDEO.JSON")

# Cinema stores the offset as a C# int; keep writes inside that range so the
# mod can always deserialize what we produce.
_INT32_MIN = -2_147_483_648
_INT32_MAX = 2_147_483_647


# ── YouTube URL parsing ──────────────────────────────────────────────────────

# Every YouTube video ID is 11 characters of URL-safe base64. Cinema's own
# regex (``\/watch\?v=([a-z0-9_-]*)`` in VideoConfig.cs) is looser and only
# handles one URL form, because it is re-parsing links it wrote itself. This
# is parsing whatever the user copied out of a browser or the mobile app, so
# it goes wider on the forms and stricter on the ID.
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# youtube-nocookie is what an embed copied off a third-party site carries.
_YT_HOSTS = frozenset({
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtube-nocookie.com", "www.youtube-nocookie.com",
})
_SHORT_HOSTS = frozenset({"youtu.be", "www.youtu.be"})

# Path prefixes that carry the ID as the next component.
_ID_PATH_PREFIXES = ("shorts", "embed", "live", "v")

# "90", "90s", "1m30s", "1h2m3s" — the three spellings YouTube itself emits,
# plus the bare-seconds form its share dialog uses.
_TIMESTAMP_RE = re.compile(
    r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?$", re.IGNORECASE
)


def parse_timestamp(raw: str | None) -> int | None:
    """Seconds from a ``t=``/``start=`` value, or None if it isn't one."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    match = _TIMESTAMP_RE.match(text)
    if match is None or not any(match.groups()):
        return None
    hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def parse_youtube_url(text: str | None) -> tuple[str | None, int | None]:
    """Pull ``(video_id, start_seconds)`` out of whatever the user pasted.

    Handles ``watch?v=``, ``youtu.be/``, ``shorts/``, ``embed/``, ``live/``,
    ``/v/`` and a bare 11-character ID. ``list=``/``index=`` are ignored
    outright — a playlist link points at one video and that's the one wanted.
    Returns ``(None, None)`` for anything that isn't a YouTube video link, so
    this doubles as the "should I prefill from the clipboard?" test.

    Pure: no network, no filesystem.
    """
    if not text:
        return None, None
    text = text.strip()
    if _VIDEO_ID_RE.match(text):
        return text, None

    # urlparse only finds a netloc after a "//"; a pasted "youtu.be/xyz" has
    # neither scheme nor slashes, and would otherwise parse as a bare path.
    candidate = text if "//" in text else "//" + text
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None, None

    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]

    video_id: str | None = None
    query = parse_qs(parsed.query)
    if host in _SHORT_HOSTS:
        video_id = parts[0] if parts else None
    elif host in _YT_HOSTS:
        if parts and parts[0].lower() == "watch":
            video_id = (query.get("v") or [None])[0]
        elif len(parts) >= 2 and parts[0].lower() in _ID_PATH_PREFIXES:
            video_id = parts[1]
    if video_id is None or not _VIDEO_ID_RE.match(video_id):
        return None, None

    # "?t=" is the usual spelling; "#t=" is what an old embed carries and
    # "start=" is what the embed API takes.
    start_raw = (query.get("t") or query.get("start") or [None])[0]
    if start_raw is None and parsed.fragment.startswith("t="):
        start_raw = parsed.fragment[2:]
    return video_id, parse_timestamp(start_raw)


# ── Filename derivation (must match the mod byte for byte) ───────────────────

def filter_emoji(text: str) -> str:
    """Cinema's ``Util.FilterEmoji``: strip ``\\p{Cs}``, the UTF-16 surrogates.

    C# strings are UTF-16, so an astral-plane character (emoji, and plenty of
    CJK compatibility glyphs) is stored there as a *surrogate pair* and both
    halves match ``\\p{Cs}``. Python strings hold the whole code point, so the
    equivalent is dropping every character above the BMP. Not an edge case:
    astral characters turn up constantly in YouTube music titles, and getting
    this wrong means deriving a filename the mod will never look for.
    """
    return "".join(c for c in text if ord(c) <= 0xFFFF)


def safe_filename(title: str) -> str:
    """Cinema's ``Util.ReplaceIllegalFilesystemChars``.

    ``Path.GetInvalidFileNameChars()`` plus a literal period, all to ``_`` —
    so "DECO*27 … feat. Miku" becomes "DECO_27 … feat_ Miku".
    """
    return re.sub(r'[<>:"/\\|?*.\x00-\x1f]', "_", title)


def shorten_filename(folder: Path | str, name: str) -> str:
    """Cinema's ``Util.ShortenFilename``: keep the full path under MAX_PATH.

    The mod budgets 259 characters minus the level directory, ".mp4" and the
    ".fXXXX" yt-dlp adds to format-specific parts, then truncates the name to
    fit — so a very long title produces a *different* file on disk than the
    untruncated one, and we have to truncate identically or we'd look for a
    video the mod never wrote (and write one it will never find).

    Windows-specific by origin, applied unconditionally: the point is to agree
    with the mod, not to protect our own filesystem.
    """
    allowed = 259 - len(str(folder)) - len(".mp4") - len(".fxxxx")
    if allowed >= len(name):
        return name
    return name[:allowed] if allowed > 0 else name


def derive_video_filename(folder: Path | str, title: str, video_id: str = "") -> str:
    """The exact name ``VideoConfig(YTResult, levelPath)`` would store.

    Emoji first, *then* truncate — the other order can cut a title mid-astral
    character, and the mod (which filters in ``YTResult``, before the config
    is ever built) never does that.
    """
    name = safe_filename(filter_emoji(title or "") or video_id or "video")
    name = shorten_filename(folder, name)
    if not name.endswith(".mp4"):
        name += ".mp4"
    return name


def find_config(folder: Path) -> Path | None:
    """Return the song folder's Cinema manifest path, or None if absent."""
    for name in CONFIG_NAMES:
        candidate = Path(folder) / name
        if candidate.exists():
            return candidate
    return None


def load_config(folder: Path) -> tuple[Path, dict]:
    """Return ``(path, parsed_dict)`` for the folder's Cinema manifest.

    Raises ``FileNotFoundError`` when there is no manifest and ``ValueError``
    when it isn't a JSON object — the pre-1.0 "video list" format was a JSON
    array, and rewriting one of those as an object would break the mod.
    """
    path = find_config(folder)
    if path is None:
        raise FileNotFoundError(f"No cinema-video.json in {folder}")
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(data, dict):
        raise ValueError(
            f"{path.name} is not a Cinema video config "
            f"(expected a JSON object, got {type(data).__name__})"
        )
    return path, data


def read_offset_ms(folder: Path) -> int:
    """The manifest's current offset in ms (0 when missing or unparseable)."""
    try:
        _, data = load_config(folder)
    except (OSError, ValueError):
        return 0
    return _coerce_offset(data.get("offset"))


def original_offset_ms(data: dict) -> int | None:
    """The mapper's offset before any user override, if one was recorded.

    Only present once Cinema (or this editor) has written ``userSettings``;
    for a config the user has never customised there is nothing to restore
    to and the caller should fall back to the live ``offset``.
    """
    settings = data.get("userSettings")
    if not isinstance(settings, dict):
        return None
    if settings.get("customOffset") is not True:
        return None
    raw = settings.get("originalOffset")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _coerce_offset(raw) -> int:
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def save_offset(folder: Path, offset_ms: int, *, backup: bool = True) -> Path:
    """Write ``offset_ms`` into the folder's ``cinema-video.json``.

    Backs the original up to ``cinema-video.json.bak`` on the first edit —
    the same convention ``asset_editor`` uses for covers and audio, which
    means the existing "Restore Files" context-menu action picks this up for
    free. Returns the manifest path.

    A write that wouldn't change the stored value is skipped entirely, so
    opening the editor and pressing Save without touching anything doesn't
    create a backup or rewrite the file.

    ``backup=False`` is for a config this app created from scratch (see
    ``create_config``). The backup exists to undo an edit back to what was on
    disk before we touched the song, and for a config we just wrote that is
    *nothing* — a ``.bak`` there would only offer to restore our own
    unsynced offset-0 config, which is not a state anyone wants back.
    """
    path, data = load_config(folder)

    offset = max(_INT32_MIN, min(_INT32_MAX, int(offset_ms)))
    if _coerce_offset(data.get("offset")) == offset and "offset" in data:
        return path

    _record_user_override(data)
    data["offset"] = offset

    bak = path.with_name(path.name + ".bak")
    if backup and not bak.exists():
        shutil.copy2(path, bak)

    # indent=2 matches Newtonsoft's Formatting.Indented, so a file we rewrite
    # still diffs cleanly against one Cinema wrote. ensure_ascii=False keeps
    # non-Latin video titles readable instead of exploding into \uXXXX.
    atomic_write_text(
        path, json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    )
    return path


def create_config(
    folder: Path,
    video_id: str,
    title: str,
    author: str,
    duration_s: int,
    *,
    offset_ms: int = 0,
    video_file: str | None = None,
) -> Path:
    """Write a fresh ``cinema-video.json`` for a video the user chose.

    Writes exactly the six fields ``VideoConfig(YTResult, levelPath)`` sets —
    ``videoID``, ``title``, ``author``, ``videoFile``, ``duration``,
    ``offset`` — and nothing else; Cinema fills its own defaults for every
    absent key, and inventing values for screen placement or colour correction
    would be guessing at a mapper's intent we don't have.

    **No ``configByMapper``.** That flag means "a mapper shipped this", and
    it's what gates ``save_offset``'s ``_record_user_override``. A config we
    created is the user's own, so there is no original offset to preserve —
    setting the flag would be a lie that makes the mod offer to "reset" to a
    value we invented.

    Conventions match ``save_offset``: atomic write, ``indent=2``,
    ``ensure_ascii=False``, and a one-time ``.bak`` when overwriting so
    "Restore Files" picks it up. Refuses to clobber the pre-1.0 JSON-array
    format for the same reason ``load_config`` refuses to read it.
    """
    folder = Path(folder)
    path = find_config(folder)
    if path is not None:
        # Same guard as load_config: an array is the legacy video-list format
        # and replacing it with an object is not an edit the user asked for.
        try:
            existing = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, list):
            raise ValueError(
                f"{path.name} is a legacy Cinema video list, not a video config"
            )
        bak = path.with_name(path.name + ".bak")
        if not bak.exists():
            shutil.copy2(path, bak)
    else:
        path = folder / CONFIG_NAMES[0]

    if video_file is None:
        video_file = derive_video_filename(folder, title, video_id)

    data = {
        "videoID": video_id,
        "title": title,
        "author": author,
        "videoFile": video_file,
        "duration": max(0, int(duration_s or 0)),
        "offset": max(_INT32_MIN, min(_INT32_MAX, int(offset_ms or 0))),
    }
    atomic_write_text(
        path, json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    )
    return path


def save_video_file(folder: Path, filename: str) -> Path:
    """Write ``filename`` into the folder's ``cinema-video.json`` as videoFile.

    Only used to repair a name the filesystem can't represent. Cinema takes
    ``videoFile`` verbatim (``VideoConfig.GetVideoFileName``), so a mapper who
    pastes a video title containing a slash leaves the mod downloading into a
    subfolder it never looks in again — the map reads as "not downloaded"
    in-game no matter how many times the user tries. Correcting the stored
    name is the only thing that makes the mod agree with what's on disk.

    Backed up to ``cinema-video.json.bak`` on the first edit, like
    ``save_offset``, so "Restore Files" undoes it. No ``userSettings`` is
    recorded: that flag is Cinema's marker for an *offset* the user moved off
    the mapper's value, and this isn't a preference to reset to.
    """
    path, data = load_config(folder)

    if data.get("videoFile") == filename:
        return path

    data["videoFile"] = filename

    bak = path.with_name(path.name + ".bak")
    if not bak.exists():
        shutil.copy2(path, bak)

    atomic_write_text(
        path, json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    )
    return path


def _record_user_override(data: dict) -> None:
    """Mark a mapper-provided offset as user-customised, Cinema-style.

    Mirrors ``VideoMenu.CustomizeOffset``: only configs flagged
    ``configByMapper`` get the treatment, and ``originalOffset`` captures the
    value *currently on disk* — so it's written once, on the first edit, and
    left alone afterwards rather than drifting to match each new value.
    """
    if data.get("configByMapper") is not True:
        return
    settings = data.get("userSettings")
    if not isinstance(settings, dict):
        settings = {}
        data["userSettings"] = settings
    if settings.get("customOffset") is True and "originalOffset" in settings:
        return
    settings["customOffset"] = True
    settings.setdefault("originalOffset", _coerce_offset(data.get("offset")))
