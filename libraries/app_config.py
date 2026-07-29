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
    """Persist ``cfg`` to config.json. Returns True on success."""
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
