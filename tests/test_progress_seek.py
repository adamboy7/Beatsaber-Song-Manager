"""Click-to-seek on the media player's progress bar.

Three pieces have to agree for a click on the bar to land where it looks like
it should:

* ``_seek_position`` turns a pixel offset into a song position;
* ``MediaPlayer.seek`` moves the audio there on either backend, and rebases the
  wall-clock estimate so ``elapsed_seconds`` doesn't report the old position
  until mpv catches up;
* the Visualizer notices, since its spectrum stream and Cinema video are
  positioned once at stream start and then run on their own clock.

MediaPlayer is exercised with fakes standing in for libmpv and the ffplay
subprocess — no window, no audio device, no child process.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from libraries import media_player as mp_mod
from libraries.browser_playback import _seek_position


# ── Fakes ────────────────────────────────────────────────────────────────────

class FakeMpv:
    """The handful of libmpv properties MediaPlayer touches when seeking."""

    def __init__(self, duration=200.0, time_pos=10.0):
        self.duration = duration
        self.time_pos = time_pos
        self.pause = False
        self.volume = 75
        self.idle_active = False


class FakeProc:
    """An ffplay subprocess that is alive until terminated."""

    def __init__(self, pid=4321):
        self.pid = pid
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


def make_song(tmp_path, name="song.egg"):
    audio = tmp_path / name
    audio.write_bytes(b"\x00fake")
    return SimpleNamespace(
        audio_path=audio, display_name=name, song_name=name, folder=tmp_path
    )


@pytest.fixture()
def mpv_player(tmp_path):
    """A MediaPlayer mid-playback on the mpv backend."""
    p = mp_mod.MediaPlayer()
    p._player = FakeMpv()
    p._backend = "mpv"
    p.playing_song = make_song(tmp_path)
    p._stopped = False
    p._play_start = mp_mod.time.time() - 10.0
    return p


@pytest.fixture()
def ffplay_player(tmp_path, monkeypatch):
    """A MediaPlayer mid-playback on the ffplay fallback, recording relaunches."""
    monkeypatch.setattr(mp_mod, "_suspend_process", lambda pid: True)
    monkeypatch.setattr(mp_mod, "_resume_process", lambda pid: True)
    p = mp_mod.MediaPlayer()
    p._backend = "ffplay"
    p.playing_song = make_song(tmp_path)
    p._stopped = False
    p._play_start = mp_mod.time.time() - 10.0
    p.song_duration = 200.0
    p._ffplay_proc = FakeProc()

    launches: list[float] = []

    def fake_launch(song, start):
        launches.append(start)
        p._ffplay_proc = FakeProc()
        return True

    monkeypatch.setattr(p, "_launch_ffplay", fake_launch)
    p.launches = launches
    return p


# ── Pixel → position ─────────────────────────────────────────────────────────

class TestSeekPosition:
    def test_midpoint_is_half_the_song(self):
        assert _seek_position(50, 100, 200.0) == 100.0

    def test_left_edge_is_the_start(self):
        assert _seek_position(0, 100, 200.0) == 0.0

    @pytest.mark.parametrize("x", [-30, 130])
    def test_a_drag_off_either_end_stays_in_range(self, x):
        """B1-Motion keeps firing with coordinates outside the widget."""
        assert 0.0 <= _seek_position(x, 100, 200.0) <= 200.0

    def test_an_unmapped_bar_has_no_position(self):
        """Width is 1 until Tk maps the widget; dividing by it would be junk."""
        assert _seek_position(40, 1, 200.0) == 0.0


# ── MediaPlayer.seek: mpv backend ────────────────────────────────────────────

class TestSeekOnMpv:
    def test_moves_the_playhead(self, mpv_player):
        mpv_player.seek(90.0)
        assert mpv_player._player.time_pos == 90.0

    def test_bumps_the_seek_counter(self, mpv_player):
        """The Visualizer's only signal that the audio jumped."""
        before = mpv_player.seek_id
        mpv_player.seek(90.0)
        assert mpv_player.seek_id == before + 1

    def test_does_not_look_like_a_new_session(self, mpv_player):
        """A seek is the same playback moved, so session_id must hold still."""
        before = mpv_player.session_id
        mpv_player.seek(90.0)
        assert mpv_player.session_id == before

    def test_rebases_the_wall_clock_estimate(self, mpv_player):
        """The estimate covers the window before mpv's time-pos updates.

        Without the rebase it would keep reporting the pre-seek position, and
        the Visualizer — which starts its stream from elapsed_seconds() — would
        re-sync to where the song used to be.
        """
        mpv_player._player.time_pos = None  # still seeking; no live position
        mpv_player.seek(90.0)
        assert mpv_player.elapsed_seconds() == pytest.approx(90.0, abs=0.5)

    def test_a_paused_seek_stays_paused(self, mpv_player):
        mpv_player._audio_paused = True
        mpv_player._player.pause = True
        mpv_player.seek(90.0)
        assert mpv_player._player.pause is True
        assert mpv_player.is_paused

    def test_a_paused_estimate_holds_at_the_new_position(self, mpv_player):
        """Paused, elapsed must sit still at where the scrub left it."""
        mpv_player._audio_paused = True
        mpv_player._player.time_pos = None
        mpv_player.seek(90.0)
        assert mpv_player.elapsed_seconds() == pytest.approx(90.0, abs=0.5)

    def test_scrubbing_back_from_the_end_revives_the_song(self, mpv_player):
        """keep-open parks mpv paused on the last sample with eof-reached set.

        Left alone, the queue tick reads that as "song over" and skips to the
        next track the instant the scrub lands.
        """
        mpv_player._finished = True
        mpv_player._player.pause = True
        mpv_player.seek(30.0)
        assert not mpv_player._finished
        assert mpv_player._player.pause is False

    def test_a_click_at_the_far_right_does_not_end_the_song(self, mpv_player):
        """Landing on the final sample would advance the queue, not seek."""
        mpv_player.seek(200.0)
        assert mpv_player._player.time_pos < 200.0
        assert mpv_player._player.time_pos > 199.0

    def test_a_negative_position_clamps_to_the_start(self, mpv_player):
        mpv_player.seek(-5.0)
        assert mpv_player._player.time_pos == 0.0

    def test_stopped_playback_ignores_a_seek(self, mpv_player):
        mpv_player._stopped = True
        mpv_player.seek(90.0)
        assert mpv_player._player.time_pos == 10.0
        assert mpv_player.seek_id == 0

    def test_an_empty_player_ignores_a_seek(self, mpv_player):
        mpv_player.playing_song = None
        mpv_player.seek(90.0)
        assert mpv_player.seek_id == 0

    def test_seeking_is_live(self, mpv_player):
        """So the progress bar can scrub on every motion event."""
        assert mpv_player.has_live_seek


# ── MediaPlayer.seek: ffplay fallback ────────────────────────────────────────

class TestSeekOnFfplay:
    def test_relaunches_at_the_new_position(self, ffplay_player):
        """ffplay has no live transport; -ss on a fresh process is the seek."""
        ffplay_player.seek(90.0)
        assert ffplay_player.launches == [90.0]

    def test_bumps_the_seek_counter(self, ffplay_player):
        ffplay_player.seek(90.0)
        assert ffplay_player.seek_id == 1

    def test_elapsed_follows_the_seek(self, ffplay_player):
        """There's no time_pos here — the estimate is all the bar ever reads."""
        ffplay_player.seek(90.0)
        assert ffplay_player.elapsed_seconds() == pytest.approx(90.0, abs=0.5)

    def test_a_paused_seek_does_not_start_playing(self, ffplay_player):
        """Relaunching would start audio the user deliberately stopped."""
        ffplay_player._audio_paused = True
        ffplay_player.seek(90.0)
        assert ffplay_player.launches == []
        assert ffplay_player._ffplay_pending_seek == 90.0
        assert ffplay_player.is_paused

    def test_resuming_after_a_paused_seek_starts_where_it_was_left(self, ffplay_player):
        ffplay_player._audio_paused = True
        ffplay_player.seek(90.0)
        ffplay_player.toggle_pause()
        assert ffplay_player.launches == [90.0]
        assert not ffplay_player.is_paused

    def test_a_second_paused_seek_replaces_the_first(self, ffplay_player):
        ffplay_player._audio_paused = True
        ffplay_player.seek(90.0)
        ffplay_player.seek(120.0)
        assert ffplay_player._ffplay_pending_seek == 120.0

    def test_seeking_is_not_live(self, ffplay_player):
        """So a drag previews silently and jumps once, on release."""
        assert not ffplay_player.has_live_seek

    def test_a_failed_relaunch_lets_the_queue_move_on(self, ffplay_player, monkeypatch):
        monkeypatch.setattr(ffplay_player, "_launch_ffplay", lambda song, start: False)
        ffplay_player.seek(90.0)
        assert ffplay_player.is_finished


# ── Wiring ───────────────────────────────────────────────────────────────────

class TestWiring:
    def test_the_progress_bar_is_bound_to_the_scrub_handlers(self):
        """Click, drag and release — a click-only bar can't scrub."""
        from pathlib import Path
        src = Path(mp_mod.__file__).parent.joinpath("browser_ui.py").read_text(
            encoding="utf-8"
        )
        for event, handler in (
            ("<Button-1>", "_on_progress_press"),
            ("<B1-Motion>", "_on_progress_drag"),
            ("<ButtonRelease-1>", "_on_progress_release"),
        ):
            assert f'self._player_progress.bind("{event}", self.{handler})' in src

    def test_the_visualizer_watches_the_seek_counter(self):
        """Its streams are seeked once at start and never re-sync on their own."""
        from pathlib import Path
        src = Path(mp_mod.__file__).parent.joinpath("visualizer_window.py").read_text(
            encoding="utf-8"
        )
        assert "mp.seek_id != self._current_seek_id" in src
        assert "_resync_stream_after_seek" in src
