"""visualizer_window — which Cinema entries the right-click menu offers.

The visualizer used to show "Cinema Offset…" and nothing else, so the only
songs you could act on from here were the ones already playing a video. What
these pin down is the three-state split: add when there's no config, edit and
replace when the video is downloaded, download when a config exists without
its file.

``cinema_menu_entries`` is a module-level function precisely so this needs no
window and no Tk — the visualizer class itself can't even be imported on a
headless machine (its base class is a stub).
"""

from __future__ import annotations

import pytest

from libraries import cinema_video
from libraries.visualizer_window import cinema_menu_entries

ID = "dQw4w9WgXcQ"


def _entries(song, downloading: bool = False, shift: bool = False):
    return cinema_menu_entries(song, downloading=downloading, shift_held=shift)


def _labels(song, downloading: bool = False, shift: bool = False) -> list[str]:
    return [label for label, _, _ in _entries(song, downloading, shift)]


def _enabled(song, label: str, downloading: bool = False,
             shift: bool = False) -> bool:
    return next(en for lbl, _, en in _entries(song, downloading, shift)
                if lbl == label)


def _action(song, label: str, shift: bool = False) -> str:
    return next(act for lbl, act, _ in _entries(song, shift=shift)
                if lbl == label)


@pytest.fixture()
def configured(song_factory):
    """A song with a cinema-video.json, its video optionally downloaded.

    Each call gets its own folder so a test can build several at once.
    """
    made = 0

    def _make(downloaded: bool):
        nonlocal made
        made += 1
        folder, song = song_factory(f"cfg{made} (Test Song - Test Mapper)")
        cinema_video.create_config(folder, ID, "Some Video", "Chan", 212)
        if downloaded:
            (folder / "Some Video.mp4").write_bytes(b"\x00fake")
        song._parse()
        assert song.has_playable_cinema_video is downloaded
        return folder, song
    return _make


# ── The three states ─────────────────────────────────────────────────────────

def test_no_config_offers_only_add(song_factory):
    _, song = song_factory()
    assert _labels(song) == ["Add Cinema Video…"]


def test_downloaded_video_offers_the_offset_editor(configured):
    _, song = configured(True)
    assert _labels(song) == ["Cinema Offset…"]


def test_shift_adds_replace_below_the_offset_editor(configured):
    _, song = configured(True)
    assert _labels(song, shift=True) == ["Cinema Offset…", "Replace Cinema Video…"]


def test_config_without_its_video_offers_download(configured):
    _, song = configured(False)
    assert _labels(song) == ["Download Video"]


def test_download_is_not_offered_once_the_video_is_there(configured):
    _, song = configured(True)
    assert "Download Video" not in _labels(song)


def test_add_is_not_offered_over_an_existing_config(configured):
    """It would overwrite the mapper's manifest without the replace warning."""
    for downloaded in (True, False):
        _, song = configured(downloaded)
        for shift in (False, True):
            assert "Add Cinema Video…" not in _labels(song, shift=shift)


def test_replace_is_hidden_without_shift(configured):
    """It overwrites a mapper's screen placement and colour correction, and it
    sits right under the entry most right-clicks here are aiming for."""
    _, song = configured(True)
    assert "Replace Cinema Video…" not in _labels(song)


def test_config_without_a_video_id_offers_nothing(configured):
    """A manifest we can neither download from nor safely overwrite."""
    folder, song = configured(False)
    (folder / "cinema-video.json").write_text('{"videoFile": "x.mp4"}')
    song._parse()
    assert song.has_cinema_video and not song.cinema_video_id
    assert _entries(song) == []


# ── Actions ──────────────────────────────────────────────────────────────────

def test_replace_is_a_distinct_action_from_add(configured, song_factory):
    """They both end at ``_add_cinema_video``, but only replace passes the flag
    that fires the "this overwrites the mapper's config" confirmation."""
    _, with_video = configured(True)
    _, without = song_factory("plain (Test Song - Test Mapper)")
    assert _action(with_video, "Replace Cinema Video…", shift=True) == "replace"
    assert _action(without, "Add Cinema Video…") == "add"


def test_every_action_has_a_handler():
    """The window maps action keys to methods; an unknown key is a KeyError at
    right-click time, i.e. in front of the user."""
    import re
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent
              / "libraries" / "visualizer_window.py").read_text(encoding="utf-8")
    block = source.split("actions = {", 1)[1].split("}", 1)[0]
    handled = set(re.findall(r'"(\w+)":', block))
    produced = {"offset", "add", "replace", "download"}
    assert produced <= handled


# ── In-flight downloads ──────────────────────────────────────────────────────

def test_add_greys_out_while_a_download_runs(song_factory):
    _, song = song_factory()
    assert _enabled(song, "Add Cinema Video…", downloading=True) is False


def test_download_greys_out_while_one_is_already_running(configured):
    _, song = configured(False)
    assert _enabled(song, "Download Video", downloading=True) is False


def test_replace_greys_out_while_a_download_runs(configured):
    _, song = configured(True)
    assert _enabled(song, "Replace Cinema Video…",
                    downloading=True, shift=True) is False


def test_offset_stays_available_during_a_download(configured):
    """It rewrites the config that's already on disk and starts nothing —
    greying it out would remove the one thing still worth doing."""
    _, song = configured(True)
    assert _enabled(song, "Cinema Offset…", downloading=True) is True


def test_nothing_is_greyed_out_when_no_download_is_running(configured, song_factory):
    _, plain = song_factory("plain (Test Song - Test Mapper)")
    _, undownloaded = configured(False)
    _, playable = configured(True)
    for song in (plain, undownloaded, playable):
        for shift in (False, True):
            assert all(en for _, _, en in _entries(song, shift=shift))


# ── Shift only affects replace ───────────────────────────────────────────────

def test_download_is_unaffected_by_shift(configured):
    """Fetching a video the manifest already names isn't destructive — a user
    who has never held Shift still finds it."""
    _, song = configured(False)
    assert _labels(song) == _labels(song, shift=True) == ["Download Video"]


def test_add_is_unaffected_by_shift(song_factory):
    _, song = song_factory()
    assert _labels(song) == _labels(song, shift=True) == ["Add Cinema Video…"]


def test_replace_is_the_only_entry_shift_reveals(configured, song_factory):
    _, playable = configured(True)
    _, undownloaded = configured(False)
    _, plain = song_factory("plain (Test Song - Test Mapper)")
    revealed = {
        song.folder.name: set(_labels(song, shift=True)) - set(_labels(song))
        for song in (playable, undownloaded, plain)
    }
    assert set().union(*revealed.values()) == {"Replace Cinema Video…"}
