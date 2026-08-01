"""The ⚠ ffmpeg state: when it triggers, and what the dialog says.

The bundle ships ffmpeg, ffprobe and ffplay together and each has a distinct
job — ffmpeg converts (including on the mpv fallback path), ffprobe reads
durations the built-in reader can't, and ffplay is the playback backend when
libmpv is unavailable. So a lone ffmpeg is an incomplete install whichever
companion is absent, not just when ffprobe is.

Both entry points are covered as pure functions so they're testable without Tk:
``missing_ffmpeg_companions`` (the trigger) and ``ffmpeg_incomplete_message``
(the text).
"""

from __future__ import annotations

import pytest

from libraries import browser_ui


def _installed(monkeypatch, *, ffprobe=True, ffplay=True):
    """Pretend the named companions are/aren't next to a found ffmpeg."""
    from libraries import audio_utils

    monkeypatch.setattr(audio_utils, "find_ffprobe",
                        lambda: "/bin/ffprobe" if ffprobe else None)
    monkeypatch.setattr(audio_utils, "find_ffplay",
                        lambda: "/bin/ffplay" if ffplay else None)


class TestTrigger:
    def test_complete_bundle_is_not_flagged(self, monkeypatch):
        _installed(monkeypatch)
        assert browser_ui.missing_ffmpeg_companions() == []

    def test_missing_ffprobe_is_flagged(self, monkeypatch):
        _installed(monkeypatch, ffprobe=False)
        assert browser_ui.missing_ffmpeg_companions() == ["ffprobe"]

    def test_missing_ffplay_is_flagged(self, monkeypatch):
        """The case this used to miss: ffprobe present, no player."""
        _installed(monkeypatch, ffplay=False)
        assert browser_ui.missing_ffmpeg_companions() == ["ffplay"]

    def test_ffmpeg_alone_reports_both(self, monkeypatch):
        _installed(monkeypatch, ffprobe=False, ffplay=False)
        assert browser_ui.missing_ffmpeg_companions() == ["ffprobe", "ffplay"]


class TestMessage:
    def test_nothing_missing_has_nothing_to_say(self):
        assert browser_ui.ffmpeg_incomplete_message([]) == ""

    @pytest.mark.parametrize("missing,other", [("ffprobe", "ffplay"),
                                               ("ffplay", "ffprobe")])
    def test_only_the_absent_one_is_named(self, missing, other):
        """Naming a binary the user already has would send them chasing it."""
        text = browser_ui.ffmpeg_incomplete_message([missing])

        assert missing in text
        assert other not in text
        assert "Reinstall ffmpeg" in text

    def test_both_missing_names_both(self):
        text = browser_ui.ffmpeg_incomplete_message(["ffprobe", "ffplay"])

        assert "ffprobe" in text
        assert "ffplay" in text

    def test_each_one_explains_what_it_costs(self):
        """A missing player and a missing prober lose different things, so the
        text can't be one generic 'something is missing'."""
        probe = browser_ui.ffmpeg_incomplete_message(["ffprobe"])
        play = browser_ui.ffmpeg_incomplete_message(["ffplay"])

        assert "duration" in probe.lower()
        assert "libmpv" in play
        assert probe != play

    def test_unknown_names_are_ignored(self):
        """Only the two companions have copy written for them."""
        assert browser_ui.ffmpeg_incomplete_message(["ffmpeg"]) == ""
