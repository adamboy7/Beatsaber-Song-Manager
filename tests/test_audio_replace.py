"""Audio replacement editor — alignment math, the write plan, and the render.

The window is never opened here; only the free functions that decide where the
replacement sits, how long the result comes out, and what ffmpeg is asked to
do. Where ffmpeg is installed the render is also exercised for real, because
the padding rules are the whole point of the feature and a filtergraph that
merely *looks* right is worth very little — ``adelay`` without ``all=1`` and
``atrim`` without ``asetpts`` both produce plausible commands and wrong audio.

Sign conventions get more attention than their line count suggests. This
editor's offset is the opposite sense to ``cinema_offset_window``'s, on
purpose, and getting one of them backwards produces a tool that writes
confident-looking alignments which are wrong by twice the offset.
"""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from libraries import timeline
from libraries.asset_editor import (
    audio_render_command,
    build_audio_filter,
    replace_audio,
)
from libraries.audio_replace_window import (
    describe_plan,
    detected_offset_ms,
    drag_to_offset_ms,
    new_time_to_x,
    nudge_delta_ms,
    plan_replacement,
)

_HAVE_FFMPEG = shutil.which("ffmpeg") is not None
_needs_ffmpeg = pytest.mark.skipif(not _HAVE_FFMPEG, reason="ffmpeg not installed")


# ── Shared timeline helpers ──────────────────────────────────────────────────


def test_clamp_window_trims_to_the_media():
    assert timeline.clamp_window(-2.0, 10.0, 30.0) == (0.0, 8.0)
    assert timeline.clamp_window(25.0, 10.0, 30.0) == (25.0, 5.0)


def test_clamp_window_returns_none_when_out_of_range():
    assert timeline.clamp_window(40.0, 10.0, 30.0) is None
    assert timeline.clamp_window(-20.0, 10.0, 30.0) is None
    assert timeline.clamp_window(0.0, 10.0, 0.0) is None


def test_clamp_view_start_keeps_the_window_inside_the_song():
    assert timeline.clamp_view_start(5.0, 10.0, 100.0) == 0.0
    assert timeline.clamp_view_start(50.0, 10.0, 100.0) == 45.0
    assert timeline.clamp_view_start(99.0, 10.0, 100.0) == 90.0


def test_x_round_trips_through_song_time():
    x = timeline.song_time_to_x(12.5, 10.0, 90.0)
    assert timeline.x_to_song_time(x, 10.0, 90.0) == pytest.approx(12.5)


def test_drag_delta_ms_is_zero_at_zero_scale():
    """A drag before the first render must not divide by a zero scale."""
    assert timeline.drag_delta_ms(100.0, 0.0) == 0.0


# ── Sign conventions ─────────────────────────────────────────────────────────


def test_dragging_right_starts_the_replacement_later():
    """The strip follows the pointer, and the number follows the strip.

    The opposite of ``cinema_offset_window.drag_to_offset_ms``, which has to
    invert because Cinema's own menu defines +ms as "starts the video earlier".
    Here the offset is a plain "begins this far into the song", so there is
    nothing to invert against.
    """
    # 100 px right at 100 px/s is one second later.
    assert drag_to_offset_ms(0, 100.0, 100.0) == 1000
    assert drag_to_offset_ms(0, -100.0, 100.0) == -1000
    assert drag_to_offset_ms(250, 50.0, 100.0) == 750


def test_dragging_is_a_no_op_before_a_scale_exists():
    assert drag_to_offset_ms(400, 999.0, 0.0) == 400


def test_arrow_keys_move_the_offset_the_way_the_strip_moves():
    """Right arrow pushes the replacement later, i.e. raises the offset.

    Unlike the Cinema editor, the arrows and the +/- buttons agree here, since
    both mean the same thing once the offset stops being inverted.
    """
    assert nudge_delta_ms(20, 1) == 20
    assert nudge_delta_ms(20, -1) == -20
    assert nudge_delta_ms(1000, 1) == 1000


def test_new_time_to_x_slides_right_as_the_offset_grows():
    at_zero = new_time_to_x(0.0, 0, 0.0, 100.0)
    at_half = new_time_to_x(0.0, 500, 0.0, 100.0)
    assert at_half - at_zero == pytest.approx(50.0)  # 500 ms at 100 px/s


def test_new_time_to_x_agrees_with_the_song_axis():
    """A moment in the replacement lands where its song time says it should."""
    assert new_time_to_x(3.0, 250, 1.0, 50.0) == pytest.approx(
        timeline.song_time_to_x(3.25, 1.0, 50.0)
    )


def test_detected_offset_trims_a_replacement_with_a_long_lead_in():
    """A replacement whose music starts late gets pulled back, not pushed out.

    The onset is the same music in both files, so the offset that makes them
    the same instant is the difference — and a replacement with two seconds of
    room tone in front of a song that starts immediately must lose those two
    seconds rather than delay the music by them.
    """
    assert detected_offset_ms(0.0, 2.0) == -2000
    assert detected_offset_ms(2.0, 0.0) == 2000
    assert detected_offset_ms(1.5, 1.5) == 0


def test_detected_offset_is_the_inverse_of_cinemas():
    """The two editors disagree by construction; pin it so neither drifts."""
    from libraries.cinema_offset_window import (
        detected_offset_ms as cinema_detected,
    )

    assert detected_offset_ms(0.5, 2.0) == -cinema_detected(0.5, 2.0)


# ── The write plan ───────────────────────────────────────────────────────────


def test_matching_length_pads_a_short_replacement():
    plan = plan_replacement(original_s=200.0, new_s=180.0, offset_ms=0,
                            match_length=True)
    assert plan.target_duration_s == 200.0
    assert plan.result_duration_s == 200.0
    assert plan.tail_pad_s == pytest.approx(20.0)
    assert plan.tail_trim_s == 0.0


def test_matching_length_trims_a_long_replacement():
    plan = plan_replacement(original_s=200.0, new_s=240.0, offset_ms=0,
                            match_length=True)
    assert plan.target_duration_s == 200.0
    assert plan.tail_trim_s == pytest.approx(40.0)
    assert plan.tail_pad_s == 0.0


def test_a_positive_offset_pads_the_front_and_eats_into_the_tail():
    plan = plan_replacement(original_s=200.0, new_s=200.0, offset_ms=5000,
                            match_length=True)
    assert plan.lead_in_s == pytest.approx(5.0)
    assert plan.head_trim_s == 0.0
    # Pushed five seconds later against a fixed length, so five come off the end.
    assert plan.tail_trim_s == pytest.approx(5.0)


def test_a_negative_offset_trims_the_head_and_pads_the_tail():
    plan = plan_replacement(original_s=200.0, new_s=200.0, offset_ms=-3000,
                            match_length=True)
    assert plan.head_trim_s == pytest.approx(3.0)
    assert plan.lead_in_s == 0.0
    assert plan.tail_pad_s == pytest.approx(3.0)


def test_unchecked_lets_a_longer_replacement_run_past():
    plan = plan_replacement(original_s=200.0, new_s=240.0, offset_ms=0,
                            match_length=False)
    assert plan.target_duration_s is None
    assert plan.result_duration_s == pytest.approx(240.0)
    assert plan.tail_trim_s == 0.0


def test_unchecked_still_pads_a_replacement_that_would_come_out_short():
    """The checkbox governs trimming, never the padding.

    A file shorter than the one it replaced is not a stylistic choice — the
    chart still has notes scheduled after the audio stops. No setting should be
    able to produce that, so the short case ignores the checkbox entirely.
    """
    plan = plan_replacement(original_s=200.0, new_s=150.0, offset_ms=0,
                            match_length=False)
    assert plan.target_duration_s == 200.0
    assert plan.tail_pad_s == pytest.approx(50.0)


def test_unchecked_pads_when_a_late_start_is_what_shortens_it():
    """Same rule, reached by offset rather than by a short file."""
    plan = plan_replacement(original_s=200.0, new_s=200.0, offset_ms=-60_000,
                            match_length=False)
    assert plan.target_duration_s == 200.0
    assert plan.head_trim_s == pytest.approx(60.0)
    assert plan.tail_pad_s == pytest.approx(60.0)


def test_a_head_trim_past_the_end_leaves_nothing():
    plan = plan_replacement(original_s=200.0, new_s=30.0, offset_ms=-60_000,
                            match_length=True)
    assert plan.head_trim_s == 30.0  # capped at the file's own length


def test_describe_plan_names_every_edit():
    plan = plan_replacement(original_s=200.0, new_s=180.0, offset_ms=2000,
                            match_length=True)
    text = describe_plan(plan, 200.0)
    assert "silence in front" in text
    assert "silence at the end" in text
    assert "3:20" in text  # 200 s


def test_describe_plan_stays_quiet_when_nothing_changes():
    plan = plan_replacement(original_s=200.0, new_s=200.0, offset_ms=0,
                            match_length=True)
    assert describe_plan(plan, 200.0) == "Result: 3:20"


# ── Filtergraph construction ─────────────────────────────────────────────────


def test_no_offset_and_no_target_needs_no_filter():
    """The empty graph is what preserves the copy fast path for an .ogg."""
    assert build_audio_filter(0, None) == ""


def test_positive_offset_delays_every_channel():
    """``all=1`` is not optional: without it adelay leaves un-named channels
    alone, which splits a stereo track into one delayed ear and one that isn't."""
    assert build_audio_filter(1500, None) == "adelay=1500:all=1"


def test_negative_offset_trims_and_restamps():
    """``atrim`` alone keeps the original timestamps on the surviving frames,
    which re-inserts exactly the lead-in that was just cut."""
    graph = build_audio_filter(-2500, None)
    assert graph == "atrim=start=2.500000,asetpts=N/SR/TB"


def test_a_target_length_appends_apad():
    assert build_audio_filter(0, 200.0) == "apad"
    assert build_audio_filter(1000, 200.0) == "adelay=1000:all=1,apad"


def test_apad_is_never_emitted_without_a_bound():
    """``apad`` pads forever; the ``-t`` is the only thing that stops it."""
    cmd = audio_render_command("ffmpeg", "in.mp3", "out.ogg", target_duration_s=200.0)
    assert "apad" in cmd[cmd.index("-af") + 1]
    assert "-t" in cmd
    assert cmd[cmd.index("-t") + 1] == "200.000000"


def test_no_target_means_no_t_flag():
    cmd = audio_render_command("ffmpeg", "in.mp3", "out.ogg", offset_ms=500)
    assert "-t" not in cmd
    assert "apad" not in " ".join(cmd)


def test_render_command_always_encodes_vorbis_ogg():
    """Beat Saber reads OGG, and ``-vn`` drops a cover-art stream that would
    otherwise be decoded for nothing."""
    assert audio_render_command("/bin/ffmpeg", "in.mp3", "out.ogg") == [
        "/bin/ffmpeg", "-y", "-nostdin", "-i", "in.mp3",
        "-vn", "-c:a", "libvorbis", "-q:a", "5", "-f", "ogg", "out.ogg",
    ]  # no -af: there is nothing to do


# ── replace_audio: backups and fast paths ────────────────────────────────────


def _fake_run(recorder):
    def _run(cmd, **kw):
        recorder.append(cmd)
        # Write something so the zero-byte guard doesn't fire.
        Path(cmd[-1]).write_bytes(b"OggS-fake-output")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    return _run


def test_an_ogg_with_nothing_to_do_is_copied_not_re_encoded(tmp_path, monkeypatch):
    """The pre-editor behaviour, and the only path that works without ffmpeg."""
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _fake_run(calls))
    audio = tmp_path / "song.egg"
    audio.write_bytes(b"original")
    new = tmp_path / "new.ogg"
    new.write_bytes(b"replacement")

    replace_audio(audio, str(new), "")

    assert audio.read_bytes() == b"replacement"
    assert calls == []  # no ffmpeg at all


def test_an_ogg_with_an_offset_is_re_encoded(tmp_path, monkeypatch):
    """The copy fast path is conditional on there being no work to do."""
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _fake_run(calls))
    audio = tmp_path / "song.egg"
    audio.write_bytes(b"original")
    new = tmp_path / "new.ogg"
    new.write_bytes(b"replacement")

    replace_audio(audio, str(new), "ffmpeg", offset_ms=250)

    assert len(calls) == 1
    assert "adelay=250:all=1" in " ".join(calls[0])


def test_the_original_is_backed_up_once(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _fake_run(calls))
    audio = tmp_path / "song.egg"
    audio.write_bytes(b"first")
    for name, body in (("a.ogg", b"second"), ("b.ogg", b"third")):
        new = tmp_path / name
        new.write_bytes(body)
        replace_audio(audio, str(new), "")

    # The backup is the *original*, not the previous replacement — otherwise
    # Restore Files would only undo the most recent swap.
    assert (tmp_path / "song.egg.bak").read_bytes() == b"first"


def test_an_offset_that_empties_the_file_is_refused(tmp_path, monkeypatch):
    """ffmpeg exits 0 having written nothing; that must not reach the song."""
    def _run(cmd, **kw):
        Path(cmd[-1]).write_bytes(b"")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", _run)
    audio = tmp_path / "song.egg"
    audio.write_bytes(b"original")
    new = tmp_path / "new.mp3"
    new.write_bytes(b"replacement")

    with pytest.raises(RuntimeError, match="no audio left"):
        replace_audio(audio, str(new), "ffmpeg", offset_ms=-999_000)
    assert audio.read_bytes() == b"original"


def test_a_failed_conversion_leaves_the_song_alone(tmp_path, monkeypatch):
    def _run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, b"", b"bad input")

    monkeypatch.setattr(subprocess, "run", _run)
    audio = tmp_path / "song.egg"
    audio.write_bytes(b"original")
    new = tmp_path / "new.mp3"
    new.write_bytes(b"junk")

    with pytest.raises(RuntimeError, match="bad input"):
        replace_audio(audio, str(new), "ffmpeg")
    assert audio.read_bytes() == b"original"
    assert not list(tmp_path.glob("*.tmp"))  # the temp file was cleaned up


# ── Real renders ─────────────────────────────────────────────────────────────


def _sine(path: Path, seconds: float, freq: int = 440) -> Path:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
         "-ac", "2", "-ar", "44100", str(path)],
        check=True,
    )
    return path


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def _first_sound_s(path: Path) -> float | None:
    """Where audible sound starts, via the same detector the editor uses."""
    from libraries.audio_utils import first_sound_offset_s

    return first_sound_offset_s(path, duration_s=_duration(path))


@_needs_ffmpeg
def test_a_short_replacement_comes_out_at_the_original_length(tmp_path):
    audio = _sine(tmp_path / "song.ogg", 6.0)
    new = _sine(tmp_path / "new.wav", 3.0, freq=660)

    replace_audio(audio, str(new), "ffmpeg", target_duration_s=6.0)

    assert _duration(audio) == pytest.approx(6.0, abs=0.15)


@_needs_ffmpeg
def test_a_long_replacement_is_cut_back_to_the_original_length(tmp_path):
    audio = _sine(tmp_path / "song.ogg", 4.0)
    new = _sine(tmp_path / "new.wav", 9.0, freq=660)

    replace_audio(audio, str(new), "ffmpeg", target_duration_s=4.0)

    assert _duration(audio) == pytest.approx(4.0, abs=0.15)


@_needs_ffmpeg
def test_a_positive_offset_writes_real_silence_in_front(tmp_path):
    """The headline case: the replacement starts later, and the gap is silent.

    Measured with the detector rather than by reading the filtergraph back,
    because ``adelay`` without ``all=1`` produces a file that passes every
    duration check and is silent in one ear only.
    """
    audio = _sine(tmp_path / "song.ogg", 8.0)
    new = _sine(tmp_path / "new.wav", 8.0, freq=660)

    replace_audio(audio, str(new), "ffmpeg", offset_ms=2000, target_duration_s=8.0)

    assert _first_sound_s(audio) == pytest.approx(2.0, abs=0.2)
    assert _duration(audio) == pytest.approx(8.0, abs=0.15)


@_needs_ffmpeg
def test_a_negative_offset_removes_the_replacements_lead_in(tmp_path):
    """A cover with two seconds of room tone lines up with a song that doesn't."""
    audio = _sine(tmp_path / "song.ogg", 8.0)
    lead_in = tmp_path / "new.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo:d=2",
         "-f", "lavfi", "-i", "sine=frequency=660:duration=6",
         "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1",
         str(lead_in)],
        check=True,
    )
    assert _first_sound_s(lead_in) == pytest.approx(2.0, abs=0.2)

    offset = detected_offset_ms(0.0, 2.0)
    assert offset == -2000
    replace_audio(audio, str(lead_in), "ffmpeg",
                  offset_ms=offset, target_duration_s=8.0)

    # The trimmed result starts immediately, like the song it replaced, and the
    # tail it lost to the trim came back as padding.
    assert (_first_sound_s(audio) or 0.0) < 0.2
    assert _duration(audio) == pytest.approx(8.0, abs=0.15)


@_needs_ffmpeg
def test_the_end_to_end_plan_matches_what_gets_written(tmp_path):
    """Whatever the plan promised the user is what lands on disk."""
    audio = _sine(tmp_path / "song.ogg", 10.0)
    new = _sine(tmp_path / "new.wav", 6.0, freq=660)

    plan = plan_replacement(original_s=10.0, new_s=6.0, offset_ms=1500,
                            match_length=True)
    replace_audio(audio, str(new), "ffmpeg", offset_ms=1500,
                  target_duration_s=plan.target_duration_s)

    assert plan.lead_in_s == pytest.approx(1.5)
    assert plan.tail_pad_s == pytest.approx(2.5)  # 10 - (1.5 + 6)
    assert _first_sound_s(audio) == pytest.approx(plan.lead_in_s, abs=0.2)
    assert _duration(audio) == pytest.approx(plan.result_duration_s, abs=0.15)


def _click_track(path: Path, silence_s: float, tone_s: float,
                 trailing_s: float = 0.0, freq: int = 880) -> Path:
    """Silence, then a tone, then optional silence — a locatable landmark."""
    inputs = [f"anullsrc=r=44100:cl=stereo:d={silence_s}",
              f"sine=frequency={freq}:duration={tone_s}"]
    if trailing_s > 0:
        inputs.append(f"anullsrc=r=44100:cl=stereo:d={trailing_s}")
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y"]
    for src in inputs:
        cmd += ["-f", "lavfi", "-i", src]
    joined = "".join(f"[{i}:a]" for i in range(len(inputs)))
    cmd += ["-filter_complex", f"{joined}concat=n={len(inputs)}:v=0:a=1",
            "-ac", "2", "-ar", "44100", str(path)]
    subprocess.run(cmd, check=True)
    return path


@_needs_ffmpeg
def test_a_cover_with_a_different_lead_in_lands_on_the_songs_downbeat(tmp_path):
    """The whole feature, end to end, on the case it was built for.

    A cover of the same song that starts 2.5 s later than the map's audio and
    runs 4 s shorter. Detection finds the offset, the render applies it, and
    the music has to come out where the *chart* expects it — at 3 s — with the
    file still as long as the one it replaced. Getting the sign backwards would
    put the downbeat at 8 s here and pass every duration assertion on the way.
    """
    song = _click_track(tmp_path / "song.ogg", silence_s=3.0, tone_s=2.0,
                        trailing_s=15.0)
    cover = _click_track(tmp_path / "cover.wav", silence_s=5.5, tone_s=2.0,
                         trailing_s=8.5, freq=660)

    original_len, cover_len = _duration(song), _duration(cover)
    assert cover_len < original_len  # shorter, so the tail needs padding

    offset = detected_offset_ms(_first_sound_s(song), _first_sound_s(cover))
    assert offset == pytest.approx(-2500, abs=100)  # trim the cover's lead-in

    plan = plan_replacement(original_len, cover_len, offset, match_length=True)
    replace_audio(song, str(cover), "ffmpeg", offset_ms=offset,
                  target_duration_s=plan.target_duration_s)

    assert _first_sound_s(song) == pytest.approx(3.0, abs=0.2)
    assert _duration(song) == pytest.approx(original_len, abs=0.2)
    assert (tmp_path / "song.ogg.bak").exists()


# ── Preview mixing ───────────────────────────────────────────────────────────


def _pcm(ms: int, value: int = 1000) -> bytes:
    from libraries.cinema_preview import BYTES_PER_MS

    frame = value.to_bytes(2, "little", signed=True) * 2  # stereo s16le
    return frame * (ms * BYTES_PER_MS // len(frame))


def test_mix_ab_solo_modes_ignore_the_other_track():
    from libraries.audio_ab_preview import MODE_NEW, MODE_ORIGINAL, mix_ab

    original, new = _pcm(100, 1000), _pcm(100, 2000)
    assert mix_ab(original, new, 0, MODE_ORIGINAL) == original
    assert mix_ab(original, new, 0, MODE_NEW) == new


def test_mix_ab_shifts_only_the_replacement():
    """The original is the reference the axis is drawn against; it never moves."""
    from libraries.audio_ab_preview import MODE_NEW, MODE_ORIGINAL, mix_ab
    from libraries.cinema_preview import BYTES_PER_MS

    original, new = _pcm(100, 1000), _pcm(100, 2000)
    shifted = mix_ab(original, new, 50, MODE_NEW)
    assert len(shifted) == len(new) + 50 * BYTES_PER_MS
    assert shifted[: 50 * BYTES_PER_MS] == b"\0" * (50 * BYTES_PER_MS)
    # Same offset, soloed to the original: unchanged.
    assert mix_ab(original, new, 50, MODE_ORIGINAL) == original


def test_mix_ab_negative_offset_trims_the_head():
    from libraries.audio_ab_preview import MODE_NEW, mix_ab
    from libraries.cinema_preview import BYTES_PER_MS

    new = _pcm(100, 2000)
    trimmed = mix_ab(_pcm(100), new, -30, MODE_NEW)
    assert len(trimmed) == len(new) - 30 * BYTES_PER_MS


def test_mix_ab_both_keeps_the_longer_track_whole():
    """Summing unequal buffers truncates to the shorter one, inaudibly."""
    from libraries.audio_ab_preview import MODE_BOTH, mix_ab

    mixed = mix_ab(_pcm(200, 1000), _pcm(50, 2000), 0, MODE_BOTH)
    assert len(mixed) == len(_pcm(200))


def test_bake_key_ignores_the_offset_when_soloed_to_the_original():
    """Nudging while auditioning the reference must not restart playback for a
    file that would come out byte-identical."""
    from libraries.audio_ab_preview import MODE_BOTH, MODE_ORIGINAL, bake_key

    assert bake_key(0, MODE_ORIGINAL) == bake_key(5000, MODE_ORIGINAL)
    assert bake_key(0, MODE_BOTH) != bake_key(5000, MODE_BOTH)


@_needs_ffmpeg
def test_the_preview_mix_is_playable_stereo(tmp_path):
    """A round trip through the real decoder, since the mix is handed to mpv."""
    from libraries.audio_ab_preview import MODE_BOTH, mix_ab
    from libraries.cinema_preview import CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH, PcmSource

    original = PcmSource.decode(_sine(tmp_path / "a.wav", 2.0))
    new = PcmSource.decode(_sine(tmp_path / "b.wav", 2.0, freq=660))
    data = mix_ab(original.pcm, new.pcm, 250, MODE_BOTH)

    out = tmp_path / "mix.wav"
    with wave.open(str(out), "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(data)

    # 2 s of original against 2 s of replacement pushed 250 ms later.
    assert _duration(out) == pytest.approx(2.25, abs=0.05)
    assert any(data)  # not silence
