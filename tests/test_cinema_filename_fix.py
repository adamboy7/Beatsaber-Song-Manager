"""browser_actions — the offer to repair an impossible cinema videoFile.

Cinema stores ``videoFile`` verbatim, so a mapper who pastes a video title
containing a slash (22e58, "…禁止令 / 草薙寧々…") leaves the mod downloading
into a subfolder it never looks in again: in-game the map reads as "not
downloaded" however many times it's retried. We can play it either way, but
only rewriting the manifest fixes the mod — and that edits the mapper's file,
so the user is asked first. Exercised against the mixin directly; no window is
ever opened.
"""

from __future__ import annotations

import json

import pytest

from conftest import make_song_folder, v2_info

from libraries import browser_actions, cinema_video
from libraries.browser_actions import BrowserActionsMixin
from libraries.song_data import SongInfo

RAW = "徳川カップヌードル禁止令 / 草薙寧々.mp4"
FIXED = "徳川カップヌードル禁止令 _ 草薙寧々.mp4"
# What yt-dlp actually wrote: the folder component's trailing space became "#"
# and the filename's leading space was dropped.
STRAY_DIR = "徳川カップヌードル禁止令#"
STRAY_FILE = "草薙寧々.mp4"


@pytest.fixture
def actions():
    """The mixin without SongBrowser — none of its Tk state is touched here."""
    return BrowserActionsMixin.__new__(BrowserActionsMixin)


@pytest.fixture
def answers(monkeypatch):
    """Record dialogs and script the Yes/No answer."""
    log = {"asked": [], "errors": [], "reply": True}
    monkeypatch.setattr(
        browser_actions.dialogs, "ask_yes_no",
        lambda title, message, **kw: (log["asked"].append(message), log["reply"])[1],
    )
    monkeypatch.setattr(
        browser_actions.dialogs, "show_error",
        lambda title, message, **kw: log["errors"].append(message),
    )
    return log


def _song(tmp_path, video_file=RAW, *, stray=False) -> SongInfo:
    folder = make_song_folder(tmp_path, "22e58 (Tokugawa - Ge2toro)", v2_info())
    (folder / "cinema-video.json").write_text(
        json.dumps({
            "videoID": "jPXAgWkqbo4", "videoFile": video_file,
            "offset": -1612, "duration": 211, "configByMapper": True,
        }),
        encoding="utf-8",
    )
    if stray:
        (folder / STRAY_DIR).mkdir()
        (folder / STRAY_DIR / STRAY_FILE).write_bytes(b"\x00")
    return SongInfo(folder)


def _manifest(song) -> dict:
    return json.loads(
        (song.folder / "cinema-video.json").read_text(encoding="utf-8")
    )


def test_accepting_rewrites_the_manifest(tmp_path, actions, answers):
    song = _song(tmp_path)
    actions._offer_cinema_filename_fix(song)
    assert answers["asked"]
    assert _manifest(song)["videoFile"] == FIXED
    assert song.cinema_video_file_raw == FIXED
    assert not song.has_illegal_cinema_filename
    # The mapper's other fields are untouched.
    assert _manifest(song)["offset"] == -1612


def test_declining_leaves_the_config_alone(tmp_path, actions, answers):
    answers["reply"] = False
    song = _song(tmp_path)
    actions._offer_cinema_filename_fix(song)
    assert _manifest(song)["videoFile"] == RAW
    assert song.has_illegal_cinema_filename


def test_a_legal_filename_is_never_questioned(tmp_path, actions, answers):
    song = _song(tmp_path, video_file="clip.mp4")
    actions._offer_cinema_filename_fix(song)
    assert answers["asked"] == []
    assert _manifest(song)["videoFile"] == "clip.mp4"


def test_an_already_downloaded_video_is_moved_not_refetched(
    tmp_path, actions, answers
):
    song = _song(tmp_path, stray=True)
    assert song.cinema_video_path == song.folder / STRAY_DIR / STRAY_FILE
    actions._offer_cinema_filename_fix(song)
    assert song.cinema_video_path == song.folder / FIXED
    assert song.has_playable_cinema_video
    assert not (song.folder / STRAY_DIR).exists()  # empty leftover cleaned up
    assert "re-downloaded" in answers["asked"][0]


def test_declining_keeps_the_downloaded_video_where_it_is(
    tmp_path, actions, answers
):
    answers["reply"] = False
    song = _song(tmp_path, stray=True)
    actions._offer_cinema_filename_fix(song)
    assert (song.folder / STRAY_DIR / STRAY_FILE).exists()
    assert song.has_playable_cinema_video  # still plays here, just not in-game


def test_a_subfolder_with_other_files_survives(tmp_path, actions, answers):
    song = _song(tmp_path, stray=True)
    (song.folder / STRAY_DIR / "notes.txt").write_text("keep me")
    actions._offer_cinema_filename_fix(song)
    assert song.cinema_video_path == song.folder / FIXED
    assert (song.folder / STRAY_DIR / "notes.txt").exists()


def test_a_failed_rewrite_is_reported(tmp_path, actions, answers, monkeypatch):
    song = _song(tmp_path)
    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(cinema_video, "save_video_file", boom)
    actions._offer_cinema_filename_fix(song)
    assert answers["errors"] and "disk full" in answers["errors"][0]
