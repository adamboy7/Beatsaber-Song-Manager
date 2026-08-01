"""MediaPlayer.refresh_duration — picking up a probe tool installed mid-playback.

Without this the progress bar stays blank until the next track: play() reads the
duration once and the tick only re-reads MediaPlayer.song_duration, so a
freshly-installed ffprobe never applies to the song already playing.

MediaPlayer is constructed without touching libmpv or any subprocess: the method
under test only needs playing_song, session_id and the duration probe.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from libraries import media_player as mp_mod


@pytest.fixture()
def player():
    return mp_mod.MediaPlayer()


def song(tmp_path, name="song.egg"):
    audio = tmp_path / name
    audio.write_bytes(b"\x00fake")
    return SimpleNamespace(
        audio_path=audio, display_name=name, song_name=name, folder=tmp_path
    )


@pytest.fixture()
def probe(monkeypatch):
    """Stand in for get_audio_duration, recording calls."""
    def _install(result, on_call=None):
        calls: list = []

        def fake(path):
            calls.append(path)
            if on_call is not None:
                on_call()
            return result() if callable(result) else result

        monkeypatch.setattr(mp_mod, "get_audio_duration", fake)
        return calls

    return _install


class TestRefreshDuration:
    def test_adopts_a_newly_available_duration(self, tmp_path, player, probe):
        """The reported symptom: ffprobe lands, the bar should fill in now."""
        probe(123.5)
        player.playing_song = song(tmp_path)
        player.song_duration = None

        assert player.refresh_duration() == 123.5
        assert player.song_duration == 123.5

    def test_probes_the_playing_song(self, tmp_path, player, probe):
        s = song(tmp_path)
        calls = probe(10.0)
        player.playing_song = s

        player.refresh_duration()

        assert calls == [s.audio_path]

    def test_no_song_is_a_no_op(self, player, probe):
        calls = probe(10.0)
        player.playing_song = None

        assert player.refresh_duration() is None
        assert calls == []

    def test_song_without_audio_is_a_no_op(self, tmp_path, player, probe):
        calls = probe(10.0)
        player.playing_song = SimpleNamespace(audio_path=None)

        assert player.refresh_duration() is None
        assert calls == []

    def test_failed_probe_leaves_the_previous_value(self, tmp_path, player, probe):
        probe(None)
        player.playing_song = song(tmp_path)
        player.song_duration = 42.0

        assert player.refresh_duration() is None
        assert player.song_duration == 42.0

    def test_zero_duration_is_not_adopted(self, tmp_path, player, probe):
        """A 0 length is a failed read, not a zero-length song."""
        probe(0.0)
        player.playing_song = song(tmp_path)
        player.song_duration = 42.0

        assert player.refresh_duration() is None
        assert player.song_duration == 42.0


class TestSessionRace:
    def test_result_is_discarded_if_the_song_changed(self, tmp_path, player, probe):
        """A probe can take seconds; by then the user may have skipped track."""
        first = song(tmp_path, "first.egg")
        second = song(tmp_path, "second.egg")
        player.playing_song = first

        def swap_track():
            player.playing_song = second
            player.session_id += 1

        probe(99.0, on_call=swap_track)
        player.song_duration = None

        assert player.refresh_duration() == 99.0  # still reported to the caller
        assert player.song_duration is None, "stale duration stamped onto new song"

    def test_result_is_kept_when_the_song_is_unchanged(self, tmp_path, player, probe):
        probe(55.0)
        player.playing_song = song(tmp_path)

        player.refresh_duration()

        assert player.song_duration == 55.0

    def test_safe_to_call_from_a_worker_thread(self, tmp_path, player, probe):
        """It's called off the UI thread precisely because probes block."""
        probe(77.0)
        player.playing_song = song(tmp_path)
        done = threading.Event()

        def worker():
            player.refresh_duration()
            done.set()

        threading.Thread(target=worker, daemon=True).start()

        assert done.wait(timeout=5)
        assert player.song_duration == 77.0


class TestQueueDurationCache:
    def test_invalidate_durations_clears_only_durations(self):
        """A row whose probe failed caches None and never retries, so the cache
        has to be dropped for it to pick up a newly-installed tool."""
        from libraries.queue_window import QueueWindow

        if not isinstance(QueueWindow, type):
            # conftest stubs tkinter on a headless machine, which makes every
            # widget class a MagicMock — including the base QueueWindow derives
            # from, so QueueWindow itself never becomes a real class and has no
            # real method to call unbound. Checked directly rather than by
            # probing for an import, so the skip states the actual precondition.
            pytest.skip("Tk is stubbed here, so QueueWindow isn't a real class")

        win = SimpleNamespace(
            _durations={"a": None, "b": 12.0}, _thumbnails={"a": object()}
        )

        QueueWindow.invalidate_durations(win)

        assert win._durations == {}
        assert win._thumbnails != {}, "thumbnails are unrelated and cost more to rebuild"
