"""Per-user application-data directory and persistent configuration.

This module owns the Song Manager's own writable home — a per-user directory
outside the (possibly read-only, PyInstaller-frozen) application folder. It
stores:

  • ``config.json`` — remembers the user's CustomLevels folder so the game
    only has to be located once.
  • ``.bsm_hash_cache.json`` — the song-hash sidecar cache (see song_data.py).
  • fallback copies of ``yt-dlp`` and ``libmpv`` when they can't live beside
    the app or in Beat Saber's own Libs folder.

Kept dependency-light on purpose (only ``platform_utils`` + stdlib) so it can
be imported from anywhere without risking a circular import.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from libraries import platform_utils

APP_DIR_NAME = "BeatSaberSongManager"
CONFIG_FILENAME = "config.json"
HASH_CACHE_FILENAME = ".bsm_hash_cache.json"

STARTUP_WINDOW_KEYS = ("open_queue_on_startup", "open_visualizer_on_startup")

VIDEO_QUALITY_KEY = "cinema_video_quality"
VIDEO_QUALITIES = ("720", "1080", "max")
DEFAULT_VIDEO_QUALITY = "720"

# Cache the resolved directory once found — the location never changes within a
# run and this avoids re-running the mkdir on every hash-cache / binary probe.
_app_data_dir: Path | None = None


def app_data_dir() -> Path:
    """Return (creating if needed) the per-user application-data directory.

    Windows: ``%APPDATA%\\BeatSaberSongManager``
    macOS:   ``~/Library/Application Support/BeatSaberSongManager``
    Linux:   ``$XDG_CONFIG_HOME/BeatSaberSongManager`` (default ``~/.config/…``)
    """
    global _app_data_dir
    if _app_data_dir is not None:
        return _app_data_dir

    if platform_utils.IS_WINDOWS:
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    elif platform_utils.IS_MAC:
        root = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        root = Path(base) if base else Path.home() / ".config"

    d = root / APP_DIR_NAME
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # best-effort; callers still get a usable path to try
    _app_data_dir = d
    return d


def config_path() -> Path:
    return app_data_dir() / CONFIG_FILENAME


def hash_cache_path() -> Path:
    """Location of the shared song-hash sidecar cache."""
    return app_data_dir() / HASH_CACHE_FILENAME


def load_config() -> dict:
    """Return the parsed config dict, or an empty dict if missing/unreadable."""
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_config(cfg: dict) -> bool:
    """Persist ``cfg`` to config.json, seeding the UI-less defaults.

    The two ``*_on_startup`` keys and ``cinema_video_quality`` are the
    preferences with no control in the UI, so being present in the file is how
    anyone discovers they exist. Seeding them here rather than in a separate
    first-run step means they land on whichever write happens first — in
    practice the ``set_custom_levels`` call that creates config.json while
    resolving the library folder.

    Only *absent* keys are filled, so this never overwrites a choice someone
    has already made, and it is a seed rather than a migration: no version
    number, nothing to run once, and an older config picks the keys up the next
    time anything is saved.
    """
    cfg = {
        **{key: False for key in STARTUP_WINDOW_KEYS},
        VIDEO_QUALITY_KEY: DEFAULT_VIDEO_QUALITY,
        **cfg,
    }
    try:
        config_path().write_text(
            json.dumps(cfg, indent=2), encoding="utf-8"
        )
        return True
    except Exception:
        return False


def get_custom_levels() -> Path | None:
    """The configured CustomLevels folder, if one is stored.

    Existence is *not* checked here — callers decide whether a stale path
    should trigger a re-scan (see Browser.py startup resolution).
    """
    val = load_config().get("custom_levels")
    if isinstance(val, str) and val.strip():
        return Path(val)
    return None


def set_custom_levels(path: Path) -> bool:
    """Record ``path`` as the CustomLevels folder, preserving other keys."""
    cfg = load_config()
    cfg["custom_levels"] = str(path)
    return save_config(cfg)


DEFAULT_VOLUME = 75


def get_volume() -> int:
    """The stored playback volume (0-100), defaulting to 75 if unset/invalid."""
    try:
        val = int(load_config().get("volume", DEFAULT_VOLUME))
    except (TypeError, ValueError):
        return DEFAULT_VOLUME
    return max(0, min(100, val))


def set_volume(level: int) -> bool:
    """Record the playback volume (clamped 0-100), preserving other keys."""
    cfg = load_config()
    cfg["volume"] = max(0, min(100, int(level)))
    return save_config(cfg)


def get_split_preview() -> bool:
    """Whether the Cinema offset editor previews in split stereo.

    Someone who syncs by ear does it that way every time, so the checkbox is
    remembered rather than reset per song. Off by default: the first thing a
    new user wants is to watch the video, not to hear two of them.
    """
    return load_config().get("cinema_split_preview") is True


def set_split_preview(enabled: bool) -> bool:
    cfg = load_config()
    cfg["cinema_split_preview"] = bool(enabled)
    return save_config(cfg)

# ── Startup windows ──────────────────────────────────────────────────────────
#
# Which auxiliary windows to open once the library has loaded. Both default to
# False, so the out-of-the-box startup is unchanged for anyone who never edits
# the file.
#
# Edited in config.json rather than through a menu, which is why ``STARTUP_
# WINDOW_KEYS`` seeds them into every config the app writes: a preference with
# no UI has to be *visible* somewhere or nobody will ever know it exists. That
# also makes them the only keys this module writes without being asked to,
# hence no setters — the app writes the default, the user writes the choice.
#
# Read only by ``SongBrowser`` (see ``_open_startup_windows``). Every headless
# CLI path exits before a ``SongBrowser`` is constructed, so ``--install``,
# ``--randomAdd`` and ``--shuffle`` never consult them — a batch job must not
# try to open a Tk window.
#
# ``is True`` rather than a truthiness test throughout: these are hand-typed,
# and ``"false"`` is a non-empty string. Reading someone's attempt to switch a
# window *off* as a request to switch it on would be a miserable bug to chase.

def get_open_queue_on_startup() -> bool:
    """Whether to open the queue window once the library has loaded."""
    return load_config().get("open_queue_on_startup") is True


def get_open_visualizer_on_startup() -> bool:
    """Whether to open the visualizer window once the library has loaded."""
    return load_config().get("open_visualizer_on_startup") is True


PREVIEW_ENGINES = ("single", "two-instance")
DEFAULT_PREVIEW_ENGINE = "single"


def get_preview_engine() -> str:
    """Which Cinema preview engine to prefer; always one of PREVIEW_ENGINES."""
    value = load_config().get("cinema_preview_engine")
    return value if value in PREVIEW_ENGINES else DEFAULT_PREVIEW_ENGINE


def set_preview_engine(engine: str) -> bool:
    """Record the preferred preview engine, ignoring unknown names."""
    if engine not in PREVIEW_ENGINES:
        return False
    cfg = load_config()
    cfg["cinema_preview_engine"] = engine
    return save_config(cfg)


# ── Cinema video quality ─────────────────────────────────────────────────────
#
# How hard to push yt-dlp when downloading a Cinema video. Like the startup
# windows this is edited in config.json rather than through a menu, hence the
# seeding in ``save_config`` — a preference with no UI has to be *visible*
# somewhere or nobody will ever know it exists.
#
# "720" is the default because it is what Cinema itself downloads in-game:
# leaving it alone means the app and the mod produce byte-comparable files, and
# a 720p H.264 stream is the one every headset decodes without breaking a
# sweat. "1080" and "max" are for people watching in the Visualizer on a
# monitor, or playing on a PC that can afford the extra decode.
#
# Whatever gets downloaded has to be H.264 by the time it reaches the song
# folder. Cinema plays through Unity's ``VideoPlayer`` (see
# Reference/BeatSaberCinema-master, CustomVideoPlayer.cs), which decodes H.264,
# H.265-with-extensions and VP8 — not VP9, not AV1. The mod's own
# ``VideoQuality.ToYoutubeDLFormat`` therefore hardcodes ``vcodec*=avc1`` for
# every hoster it allows, and its quality enum has 1440p and 2160p commented
# out entirely.
#
# YouTube publishes avc1 only up to 1080p, so "1080" is the ceiling reachable
# without re-encoding, and it is where "1080" stops. "max" goes past it by
# taking the highest resolution on offer in any codec and transcoding to H.264
# on the way in — but *only* when it has to. See ``video_download_args``.


def get_video_quality() -> str:
    """The configured download quality; always one of ``VIDEO_QUALITIES``.

    An absent, misspelled or wrong-typed value reads as the default rather
    than raising: this is hand-edited, and a typo should cost the user the
    upgrade they wanted, not the download.
    """
    value = load_config().get(VIDEO_QUALITY_KEY)
    if isinstance(value, int):  # someone typed 1080, not "1080"
        value = str(value)
    return value if value in VIDEO_QUALITIES else DEFAULT_VIDEO_QUALITY


def set_video_quality(quality: str) -> bool:
    """Record the preferred download quality, ignoring unknown values."""
    if quality not in VIDEO_QUALITIES:
        return False
    cfg = load_config()
    cfg[VIDEO_QUALITY_KEY] = quality
    return save_config(cfg)


def video_format_selector(quality: str | None = None) -> str:
    """The ``-f`` argument for yt-dlp at ``quality`` (default: configured).

    "720" and "1080" are ``/``-separated preference chains, tried left to
    right, both ending at the 720p avc1 pair the default uses — a video that
    only publishes low resolutions still downloads rather than failing the
    whole request because 1080p was asked for. Every rung constrains to avc1,
    which is Cinema's own constraint (see the module comment), so neither
    setting ever re-encodes: the stream arrives already playable.

    "max" is the one that can transcode, so it is chosen differently. Its
    rungs deliberately do *not* express "prefer higher resolution" — chains
    match left to right and cannot compare across rungs, and "the best avc1
    *unless* something higher exists" is exactly a comparison. Ordering is
    left to ``video_sort_order``; the rungs only rule things out.

    What they rule out, besides av01, is anything that would transcode past
    H.264 level 5.1 — the documented ceiling of the Media Foundation decoder
    Cinema ends up on. Level is a function of macroblocks *per second*, not
    resolution, so it is frame rate that decides where 4K becomes illegal:

        3840x2160 = 32400 MBs.  L5.1 allows 983040 MB/s.
        4K @ 30fps →  971028 MB/s → level 5.1, legal by 1.2%.
        4K @ 60fps → 1942056 MB/s → level 5.2, past the ceiling.
        1440p60    →  863136 MB/s → level 5.1, fine.

    Hence ``[height<=2160][fps<=30]`` first and a ``[height<=1440]`` rung
    behind it: a 30fps upload gets its 4K, a 60fps upload — where YouTube
    publishes every rung at 60 and there is no 4K30 to fall back to — settles
    for 1440p60 rather than producing a file the game cannot open. Both stay
    inside 5.1 whatever ffmpeg picks, because neither can exceed it.

    Note ``fps<=30`` filters the *source* stream, and the transcode preserves
    frame rate, so it is a valid proxy for the output's level.

    What it rules out is ``av01``, which is subtle enough to spell out. AV1 is
    unplayable in Cinema, and unlike VP9 it would *stay* unplayable: yt-dlp's
    ``--recode-video mp4`` re-encodes rather than remuxes (its video convertor
    deliberately omits ``-c copy``) but skips entirely when the file is already
    in the target container — and on YouTube VP9 ships in .webm while AV1 ships
    in .mp4. So the same flag transcodes VP9 to H.264 while waving AV1 straight
    through into the song folder, to be a black screen behind the map. Ruling
    av01 out costs nothing: YouTube publishes VP9 at every rung it publishes
    AV1 at, up to 4K and beyond.

    Pairing every rung with ``bestaudio[acodec*=mp4]`` matters for the same
    reason. Audio is selected independently of video, and an avc1 video merged
    with an Opus/webm track lands in an .mkv — which would then be re-encoded
    despite the video having been H.264 all along.
    """
    quality = quality or get_video_quality()
    avc1 = "bestvideo[height<={h}][vcodec*=avc1]+bestaudio[acodec*=mp4]"
    if quality == "max":
        no_av1 = "bestvideo[vcodec!*=av01]"
        return "/".join([
            f"{no_av1}[height<=2160][fps<=30]+bestaudio[acodec*=mp4]",
            f"{no_av1}[height<=1440]+bestaudio[acodec*=mp4]",
            f"{no_av1}[height<=1440]+bestaudio",
            "best[vcodec!*=av01][height<=1440]",
        ])
    if quality == "1080":
        return "/".join([avc1.format(h=1080), avc1.format(h=720)])
    return avc1.format(h=720)


def video_sort_order(quality: str | None = None) -> str | None:
    """The ``-S`` argument for yt-dlp at ``quality``, or None if not needed.

    Only "max" sorts; the capped settings say what they want in the filter and
    take yt-dlp's default ordering.

    ``res,vcodec:avc1`` is what makes "max" re-encode only when it buys
    something. Resolution sorts first, so the highest stream on offer always
    wins. Codec is consulted solely to break a *tie* at that resolution, and
    the tiebreak prefers avc1 — which is where the two cases separate:

      • A video topping out at 1080p offers both avc1 and VP9 there. The
        tiebreak takes avc1, the merge lands in .mp4, ``--recode-video mp4``
        sees the container it was asked for and skips. Nothing is re-encoded
        and the file is identical to what "1080" would have fetched.

      • A video with a 1440p or 4K stream has no avc1 at that height — YouTube
        publishes none. The resolution sort takes the VP9 stream regardless,
        it merges to .mkv, and the same flag transcodes it to H.264. Slow and
        lossy, but the alternative is throwing the extra resolution away.

    Note this is a preference, not a filter: ``vcodec:avc1`` reorders, it never
    excludes. yt-dlp's own default would tiebreak the other way (it ranks VP9
    above H.264 on efficiency), which is a defensible ranking for a downloader
    and precisely wrong here, where the cheaper codec costs a transcode.
    """
    return "res,vcodec:avc1" if (quality or get_video_quality()) == "max" else None


def video_download_args(quality: str | None = None) -> list[str]:
    """The full yt-dlp format/sort arguments for ``quality``."""
    quality = quality or get_video_quality()
    args = ["-f", video_format_selector(quality)]
    sort = video_sort_order(quality)
    if sort:
        args += ["-S", sort]
    return args
