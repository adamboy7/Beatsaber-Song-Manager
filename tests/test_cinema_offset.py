"""Cinema offset editor — timeline math and waveform cache keying.

The window itself is never opened here; only the free functions that decide
what ffmpeg is asked to render and how a drag becomes an offset.

Several cases below pin our behaviour to the Cinema mod's, quoting its source
and README (https://github.com/Kevga/BeatSaberCinema); file paths in comments
are relative to that repository. Those aren't style preferences — getting them
backwards produces an editor that writes plausible-looking offsets which
desync in-game.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from libraries import waveform
from libraries.cinema_offset_window import (
    _DRIFT_MAX_SPEED_TRIM,
    _EAR_BLEED,
    _fmt_time,
    clamp_view_start,
    clamp_window,
    drag_to_offset_ms,
    drift_speed_trim,
    nudge_delta_ms,
    pan_filter,
    song_time_to_x,
    video_time_to_x,
)


# ── clamp_window ─────────────────────────────────────────────────────────────

def test_window_fully_inside_is_unchanged():
    assert clamp_window(10.0, 5.0, 200.0) == (10.0, 5.0)


def test_window_running_off_the_start_is_trimmed():
    # A negative start is what a negative offset produces near the beginning
    # of a song; ffmpeg can't seek there, so only the overlap is rendered.
    assert clamp_window(-3.0, 10.0, 200.0) == (0.0, 7.0)


def test_window_running_off_the_end_is_trimmed():
    assert clamp_window(195.0, 10.0, 200.0) == (195.0, 5.0)


def test_window_entirely_outside_the_media_returns_none():
    # The mod ships configs with offsets of 30+ seconds ("Crab Rave" is
    # +34900 ms), so a visible song range mapping past the end of the video is
    # a real case, not a defensive hypothetical.
    assert clamp_window(220.0, 10.0, 200.0) is None
    assert clamp_window(-50.0, 10.0, 200.0) is None


def test_unknown_media_duration_returns_none():
    # cinema_video_duration_s is 0 when the manifest omits it and the probe
    # failed — better to draw nothing than to ask ffmpeg for garbage.
    assert clamp_window(0.0, 10.0, 0.0) is None


def test_degenerate_overlap_returns_none():
    assert clamp_window(199.999, 10.0, 200.0) is None


# ── drag_to_offset_ms ────────────────────────────────────────────────────────

def test_dragging_left_increases_the_offset():
    # Positive offset = video starts earlier (Cinema's own button label), so
    # pulling its waveform left must raise the number.
    assert drag_to_offset_ms(0, -100, 100.0) == 1000


def test_dragging_right_decreases_the_offset():
    assert drag_to_offset_ms(0, 100, 100.0) == -1000


def test_drag_is_relative_to_the_offset_at_press():
    assert drag_to_offset_ms(-1200, -50, 100.0) == -700


def test_drag_resolution_scales_with_zoom():
    # 900 px over the whole of a 200 s song: one pixel is ~222 ms, which is
    # why the Todo called dragging alone too coarse.
    coarse = drag_to_offset_ms(0, -1, 900 / 200)
    assert coarse == pytest.approx(222, abs=1)
    # 900 px over a 2 s window: one pixel is ~2 ms.
    fine = drag_to_offset_ms(0, -1, 900 / 2)
    assert fine == pytest.approx(2, abs=1)


def test_zero_scale_is_not_a_division_error():
    assert drag_to_offset_ms(500, -100, 0.0) == 500


# ── Arrow-key nudges ─────────────────────────────────────────────────────────
#
# The arrows move the waveform; the +/- buttons move the number. Those run in
# opposite directions, which is why this was inverted on the first pass.

def test_left_arrow_moves_the_waveform_left():
    # Left on screen = earlier in song time = a larger offset.
    assert nudge_delta_ms(20, -1) == 20


def test_right_arrow_moves_the_waveform_right():
    assert nudge_delta_ms(20, 1) == -20


@pytest.mark.parametrize("step", [20, 100, 1000])
def test_arrow_agrees_with_dragging_the_same_way(step):
    """A Left nudge and a leftward drag must move the offset the same way.

    They disagreed originally: pressing Left slid the strip right. Anything
    that fixes one direction and not the other fails here.
    """
    pps = 90.0
    drag_left = drag_to_offset_ms(0, -10, pps)      # cursor 10 px left
    key_left = nudge_delta_ms(step, -1)             # Left arrow
    assert (drag_left > 0) == (key_left > 0)

    drag_right = drag_to_offset_ms(0, 10, pps)
    key_right = nudge_delta_ms(step, 1)
    assert (drag_right < 0) == (key_right < 0)


def test_arrow_steps_are_cinemas_own():
    from libraries.cinema_offset_window import _STEPS
    # VideoMenu/VideoMenu.cs, {De,In}creaseOffset{Low,Mid,High}.
    assert _STEPS == (20, 100, 1000)


# ── clamp_view_start ─────────────────────────────────────────────────────────

def test_view_centres_on_the_target():
    assert clamp_view_start(100.0, 10.0, 200.0) == 95.0


def test_view_cannot_start_before_zero():
    assert clamp_view_start(2.0, 10.0, 200.0) == 0.0


def test_view_cannot_run_past_the_end():
    assert clamp_view_start(199.0, 10.0, 200.0) == 190.0


def test_span_longer_than_the_song_pins_to_zero():
    assert clamp_view_start(100.0, 500.0, 200.0) == 0.0


# ── Offset sign, checked against Cinema's own behaviour ──────────────────────
#
# VideoMenu/Views/video-menu.bsml labels its +20 ms button "Starts video
# earlier", and Screen/PlaybackController.cs computes
# (time * speed) + (offset / 1000f). These express that as the two directions
# a user can actually perceive, so the sign can't quietly flip.

def test_positive_offset_puts_the_video_ahead_of_the_song():
    # video_time = song_time + offset: at song time 10 s with a +2000 ms
    # offset, the frame on screen is the one 12 s into the video.
    pps, view = 100.0, 0.0
    assert video_time_to_x(12.0, 2000, view, pps) == song_time_to_x(10.0, view, pps)


def test_negative_offset_delays_the_video():
    pps, view = 100.0, 0.0
    assert video_time_to_x(8.0, -2000, view, pps) == song_time_to_x(10.0, view, pps)


def test_raising_the_offset_slides_the_video_left():
    # "Starts video earlier" — the same video moment moves toward t=0.
    before = video_time_to_x(30.0, 0, 0.0, 100.0)
    after = video_time_to_x(30.0, 500, 0.0, 100.0)
    assert after < before
    assert before - after == pytest.approx(0.5 * 100.0)  # 500 ms at 100 px/s


def test_drag_and_render_agree_on_direction():
    # Drag the strip 90 px left, then ask where a video landmark ended up: it
    # must have moved with the cursor, not against it. A sign error in either
    # function alone would show up here as movement in the wrong direction.
    pps, view = 90.0, 0.0
    landmark_v = 20.0
    before = video_time_to_x(landmark_v, 0, view, pps)
    new_offset = drag_to_offset_ms(0, -90, pps)
    after = video_time_to_x(landmark_v, new_offset, view, pps)
    assert after == pytest.approx(before - 90)


def test_offset_moves_the_video_only_not_the_song():
    pps, view = 100.0, 5.0
    song_x = song_time_to_x(10.0, view, pps)
    assert song_time_to_x(10.0, view, pps) == song_x  # no offset term at all
    assert video_time_to_x(10.0, 0, view, pps) == song_x  # zero offset aligns


def test_scrolling_the_view_shifts_both_tracks_equally():
    pps = 100.0
    delta = (song_time_to_x(10.0, 0.0, pps) - song_time_to_x(10.0, 3.0, pps))
    video_delta = (video_time_to_x(10.0, 750, 0.0, pps)
                   - video_time_to_x(10.0, 750, 3.0, pps))
    assert delta == pytest.approx(video_delta)


# ── Split-stereo preview ─────────────────────────────────────────────────────
#
# The mod's README tells users: "Sound from the video will play in your left
# ear, the map in your right ear. If the sound from the left ear is behind,
# use the '+' buttons." That instruction only holds if we assign the ears the
# same way, so the assignment is pinned here rather than left to taste.

def test_video_is_panned_left_and_song_right():
    video = pan_filter(1.0, _EAR_BLEED)
    song = pan_filter(_EAR_BLEED, 1.0)
    # c0 is the left output channel, c1 the right.
    assert "c0=0.5000" in video and "c1=0.0500" in video
    assert "c0=0.0500" in song and "c1=0.5000" in song


def test_pan_keeps_a_deliberate_bleed_into_the_other_ear():
    # Screen/PlaybackController.cs pans the map to 0.9, not 1.0: "it sounds a
    # bit more comfortable". Hard separation is fatiguing over a long session.
    assert 0 < _EAR_BLEED < 1
    assert "c1=0.0000" not in pan_filter(1.0, _EAR_BLEED)


def test_pan_normalises_the_channel_layout_first():
    # Without aformat, `pan` on a mono song references an undefined c1 and the
    # whole graph fails — songs with mono audio do exist.
    graph = pan_filter(1.0, 0.1)
    assert graph.index("aformat=channel_layouts=stereo") < graph.index("pan=")


def test_pan_filter_is_wrapped_for_mpv():
    # mpv splits af values on ','; the lavfi=[...] wrapper stops it chopping
    # the graph in half.
    graph = pan_filter(1.0, 0.1)
    assert graph.startswith("lavfi=[") and graph.endswith("]")


def test_pan_gains_are_halved_to_survive_the_downmix():
    # c0+c1 at unity would clip a loud stereo source once summed.
    assert "c0=0.5000*c0+0.5000*c1" in pan_filter(1.0, 0.0)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
@pytest.mark.parametrize("channels", [1, 2])
def test_generated_graph_is_valid_ffmpeg(tmp_path, channels):
    """The filter string is only useful if libavfilter actually accepts it."""
    src = tmp_path / "a.ogg"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-ac", str(channels), "-c:a", "libvorbis", "-f", "ogg", str(src), "-y"],
        check=True, capture_output=True,
    )
    graph = pan_filter(1.0, _EAR_BLEED).removeprefix("lavfi=[").removesuffix("]")
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(src), "-af", graph, "-f", "null", "-"],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()


# ── Drift correction ─────────────────────────────────────────────────────────

def test_video_running_ahead_is_slowed_down():
    # error > 0 means the video is ahead of where the offset says it should be.
    assert drift_speed_trim(0.05) < 1.0


def test_video_running_behind_is_sped_up():
    assert drift_speed_trim(-0.05) > 1.0


def test_no_error_means_no_trim():
    assert drift_speed_trim(0.0) == 1.0


def test_trim_is_clamped_both_ways():
    # A big error must not turn the correction into an audible scrub — the
    # caller seeks instead once the error gets that large.
    assert drift_speed_trim(10.0) == pytest.approx(1.0 - _DRIFT_MAX_SPEED_TRIM)
    assert drift_speed_trim(-10.0) == pytest.approx(1.0 + _DRIFT_MAX_SPEED_TRIM)


def test_trim_stays_inaudible_for_realistic_drift():
    # 30 ms of drift is a typical worst case between two players over a few
    # seconds; the correction for it should be a fraction of a percent.
    assert abs(drift_speed_trim(0.03) - 1.0) < 0.02


# ── Waveform cache keying ────────────────────────────────────────────────────

def test_cache_key_changes_with_every_render_parameter(tmp_path):
    src = tmp_path / "song.egg"
    src.write_bytes(b"\x00" * 64)
    base = dict(start_s=0.0, duration_s=10.0, width=900, height=100, color="#c724b1")

    def key(**overrides):
        return waveform.cache_key(src, **{**base, **overrides})

    baseline = key()
    assert key() == baseline  # stable for identical inputs
    assert key(start_s=1.0) != baseline
    assert key(duration_s=11.0) != baseline
    assert key(width=901) != baseline
    assert key(height=101) != baseline
    assert key(color="#4ec9ff") != baseline


def test_cache_key_changes_when_the_source_content_changes(tmp_path):
    src = tmp_path / "song.egg"
    src.write_bytes(b"\x00" * 64)
    args = (0.0, 10.0, 900, 100, "#c724b1")
    before = waveform.cache_key(src, *args)

    # Same path, different bytes: an asset edit replaces audio in place, so a
    # key that only covered the path would serve the old waveform forever.
    src.write_bytes(b"\x01" * 128)
    assert waveform.cache_key(src, *args) != before


def test_ffmpeg_color_translation():
    # '#' has to escape inside a filtergraph; ffmpeg's 0x form avoids that.
    assert waveform._ffmpeg_color("#c724b1") == "0xc724b1"
    assert waveform._ffmpeg_color("white") == "white"
    assert waveform._ffmpeg_color("") == "white"


def test_render_without_ffmpeg_raises_waveform_error(tmp_path, monkeypatch):
    src = tmp_path / "song.egg"
    src.write_bytes(b"\x00" * 64)
    monkeypatch.setattr(waveform, "find_ffmpeg", lambda: None)
    monkeypatch.setattr(waveform, "cache_dir", lambda: tmp_path)
    with pytest.raises(waveform.WaveformError):
        waveform.render(src)


def test_render_of_a_missing_file_raises_waveform_error(tmp_path):
    with pytest.raises(waveform.WaveformError):
        waveform.render(tmp_path / "nope.mp4")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_render_window_covers_exactly_the_requested_range(tmp_path, monkeypatch):
    """A windowed strip must show that window, not the file's whole tail.

    This is the check that catches ``-t`` drifting to the wrong side of
    ``-i``: as an output option it does not truncate the audio feeding
    ``showwavespic``, and the strip ends up covering ``start_s``..EOF at the
    wrong scale — the detail strips silently disagreeing with the range the
    overview highlights.
    """
    pytest.importorskip("PIL")
    from PIL import Image

    src = tmp_path / "song.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=1000:duration=60",
         "-af", "volume=enable='between(t,20,25)':volume=1.0,"
                "volume=enable='not(between(t,20,25))':volume=0.001",
         "-y", str(src)],
        check=True, capture_output=True,
    )

    monkeypatch.setattr(waveform, "cache_dir", lambda: tmp_path)
    width, start_s, span_s = 900, 18.0, 10.0
    png = waveform.render(src, start_s=start_s, duration_s=span_s,
                          width=width, height=96, color="#4ec9ff")

    with Image.open(png) as img:
        grey = img.convert("L")
        w, h = grey.size
        px = grey.load()
        lit = [x for x in range(w)
               if max(px[x, y] for y in range(h)) > 40]

    assert w == width
    px_per_s = width / span_s
    # The burst runs 20–25 s, i.e. 2–7 s into an 18–28 s window.
    assert lit, "the burst did not render at all"
    assert lit[0] == pytest.approx((20.0 - start_s) * px_per_s, abs=6)
    assert lit[-1] == pytest.approx((25.0 - start_s) * px_per_s, abs=6)


def test_prune_cache_keeps_the_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(waveform, "cache_dir", lambda: tmp_path)
    import os
    for i in range(6):
        png = tmp_path / f"{i}.png"
        png.write_bytes(b"x")
        os.utime(png, (1_700_000_000 + i, 1_700_000_000 + i))
    assert waveform.prune_cache(max_files=2) == 4
    assert {p.name for p in tmp_path.glob("*.png")} == {"4.png", "5.png"}


# ── Formatting ───────────────────────────────────────────────────────────────

def test_fmt_time():
    assert _fmt_time(0) == "0:00"
    assert _fmt_time(9.9) == "0:09"
    assert _fmt_time(125) == "2:05"
    assert _fmt_time(-4) == "0:00"
