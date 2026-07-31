"""Shared fixtures for the test suite.

The suite targets the UI-free logic: song parsing/hashing, search-tag
filtering, playlist matching, player-data parsing, random picks, and
filesystem helpers. It never opens a window.

Some modules under test (browser_pagination, dialogs) import ``tkinter`` at
module level even though the functions we exercise never touch it. On a
headless machine without Tk (CI containers), a minimal stub is injected so
those imports succeed; when real tkinter is available it is used untouched.

Run from the repo root:  python -m pytest tests/ -q
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import tkinter  # noqa: F401
    HAS_REAL_TK = True
except ImportError:
    HAS_REAL_TK = False
    from unittest.mock import MagicMock

    class TclError(Exception):
        """Stand-in for tkinter.TclError.

        Needs to be a real exception class, not a MagicMock: production code
        says ``except tk.TclError`` around clipboard and widget calls, and
        catching a non-exception is a TypeError at runtime.
        """

    def _make_stub(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        mod.__getattr__ = lambda attr: MagicMock(name=f"{name}.{attr}")  # PEP 562
        return mod

    for _name in ("tkinter", "tkinter.filedialog", "tkinter.font", "tkinter.ttk"):
        sys.modules.setdefault(_name, _make_stub(_name))
    sys.modules["tkinter"].TclError = TclError


# ── Song-folder factory ──────────────────────────────────────────────────────

def v2_info(
    *,
    title="Test Song",
    sub="",
    artist="Test Artist",
    mapper="Test Mapper",
    bpm=120.0,
    audio="song.egg",
    cover="cover.jpg",
    diffs=None,
) -> dict:
    """Build a v2 Info.dat dict.

    ``diffs`` is a list of (difficulty_name, custom_data_dict_or_None),
    grouped under the Standard characteristic unless an entry is a
    3-tuple (characteristic, difficulty_name, custom_data).
    """
    sets: dict[str, list] = {}
    for entry in diffs or [("Expert", None)]:
        if len(entry) == 3:
            char, name, custom = entry
        else:
            name, custom = entry
            char = "Standard"
        bm = {"_difficulty": name, "_beatmapFilename": f"{name}{char}.dat"}
        if custom is not None:
            bm["_customData"] = custom
        sets.setdefault(char, []).append(bm)
    return {
        "_version": "2.0.0",
        "_songName": title,
        "_songSubName": sub,
        "_songAuthorName": artist,
        "_levelAuthorName": mapper,
        "_beatsPerMinute": bpm,
        "_songFilename": audio,
        "_coverImageFilename": cover,
        "_difficultyBeatmapSets": [
            {"_beatmapCharacteristicName": char, "_difficultyBeatmaps": bms}
            for char, bms in sets.items()
        ],
    }


def make_song_folder(root: Path, folder_name: str, info: dict) -> Path:
    """Write a song folder: Info.dat, every referenced diff/audio/cover file."""
    folder = root / folder_name
    folder.mkdir(parents=True)
    (folder / "Info.dat").write_text(
        json.dumps(info, ensure_ascii=False), encoding="utf-8"
    )
    referenced: list[str] = []
    for bms in info.get("_difficultyBeatmapSets", []):
        for bm in bms.get("_difficultyBeatmaps", []):
            referenced.append(bm["_beatmapFilename"])
    for bm in info.get("difficultyBeatmaps", []):  # v4
        for key in ("beatmapDataFilename", "lightshowDataFilename"):
            if bm.get(key):
                referenced.append(bm[key])
    for fn in referenced:
        (folder / fn).write_text(json.dumps({"_notes": [], "file": fn}))
    for key in ("_songFilename", "_coverImageFilename"):
        if info.get(key):
            (folder / info[key]).write_bytes(b"\x00fake")
    audio_obj = info.get("audio", {})
    if audio_obj.get("songFilename"):
        (folder / audio_obj["songFilename"]).write_bytes(b"\x00fake")
    if info.get("coverImageFilename"):
        (folder / info["coverImageFilename"]).write_bytes(b"\x00fake")
    return folder


@pytest.fixture()
def song_factory(tmp_path):
    """Return (folder, SongInfo) for a v2 song built from kwargs."""
    from libraries.song_data import SongInfo

    def _make(folder_name="abc1 (Test Song - Test Mapper)", **kw):
        folder = make_song_folder(tmp_path, folder_name, v2_info(**kw))
        return folder, SongInfo(folder)

    return _make
