"""Read/write access to a song folder's ``cinema-video.json``.

``song_data`` parses this manifest for display (video id, filename, offset,
duration); this module is the only place that *writes* it, for the offset
editor.

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

Unknown keys are round-tripped untouched: Cinema writes far more fields than
we parse (screen placement, colour correction, environment modifications),
and dropping them would silently break the map.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from libraries.fs_utils import atomic_write_text

# Same spellings song_data probes for, in the same order.
CONFIG_NAMES = ("cinema-video.json", "Cinema-Video.json", "CINEMA-VIDEO.JSON")

# Cinema stores the offset as a C# int; keep writes inside that range so the
# mod can always deserialize what we produce.
_INT32_MIN = -2_147_483_648
_INT32_MAX = 2_147_483_647


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


def save_offset(folder: Path, offset_ms: int) -> Path:
    """Write ``offset_ms`` into the folder's ``cinema-video.json``.

    Backs the original up to ``cinema-video.json.bak`` on the first edit —
    the same convention ``asset_editor`` uses for covers and audio, which
    means the existing "Restore Files" context-menu action picks this up for
    free. Returns the manifest path.

    A write that wouldn't change the stored value is skipped entirely, so
    opening the editor and pressing Save without touching anything doesn't
    create a backup or rewrite the file.
    """
    path, data = load_config(folder)

    offset = max(_INT32_MIN, min(_INT32_MAX, int(offset_ms)))
    if _coerce_offset(data.get("offset")) == offset and "offset" in data:
        return path

    _record_user_override(data)
    data["offset"] = offset

    bak = path.with_name(path.name + ".bak")
    if not bak.exists():
        shutil.copy2(path, bak)

    # indent=2 matches Newtonsoft's Formatting.Indented, so a file we rewrite
    # still diffs cleanly against one Cinema wrote. ensure_ascii=False keeps
    # non-Latin video titles readable instead of exploding into \uXXXX.
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
