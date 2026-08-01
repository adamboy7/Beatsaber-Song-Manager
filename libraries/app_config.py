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
    """Persist ``cfg`` to config.json, seeding startup-window defaults.

    The two ``*_on_startup`` keys are the only preferences with no control in
    the UI, so being present in the file is how anyone discovers they exist.
    Seeding them here rather than in a separate first-run step means they land
    on whichever write happens first — in practice the ``set_custom_levels``
    call that creates config.json while resolving the library folder.

    Only *absent* keys are filled, so this never overwrites a choice someone
    has already made, and it is a seed rather than a migration: no version
    number, nothing to run once, and an older config picks the keys up the next
    time anything is saved.
    """
    cfg = {**{key: False for key in STARTUP_WINDOW_KEYS}, **cfg}
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
