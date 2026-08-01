"""Single-instance Cinema preview — timeline math, baking and mixing.

No player is ever created here. What is pinned down instead is everything the
preview can get wrong without mpv noticing: that the shift lands where the
timeline says, that the two ears are mixed at the gains Cinema documents, and
that the mix this module builds matches what ffmpeg's own pan filter produces.

The ffmpeg-backed cases at the end are skipped when it isn't installed, in
keeping with test_cinema_offset.py.
"""

from __future__ import annotations

import ast
import pathlib
import shutil
import subprocess
import time
import wave

import pytest

from libraries.cinema_offset_window import _EAR_BLEED, pan_filter
from libraries.cinema_preview import (
    BAKE_BUCKET_MS,
    BYTES_PER_MS,
    CHANNELS,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    AudioTracks,
    PcmSource,
    PreviewError,
    audio_track_ids,
    bake_shift_ms,
    describe_tracks,
    master_from_song,
    pan_chain,
    residual_delay_ms,
    song_from_master,
    mix_preview,
    pan_ears,
    wav_bytes_for_shift,
)


# ── ffmpeg-backed helpers ────────────────────────────────────────────────────
# Defined up here because cases throughout the file use them; the ffmpeg ones
# are skipped when it isn't installed, in keeping with test_cinema_offset.py.

_NO_FFMPEG = shutil.which("ffmpeg") is None


def _sine(path, seconds=2.0, channels=2, freq=440):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={seconds}",
         "-ac", str(channels), "-c:a", "libvorbis", "-f", "ogg", str(path), "-y"],
        check=True, capture_output=True,
    )
    return path


def _lead_in(path, silence_s, tone_s=1.0, container="ogg", codec="libvorbis"):
    """A file that is silent for ``silence_s`` and then plays a tone."""
    subprocess.run(
        ["ffmpeg", "-v", "error",
         "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={silence_s}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={tone_s}",
         "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[a]",
         "-map", "[a]", "-c:a", codec, "-f", container, str(path), "-y"],
        check=True, capture_output=True,
    )
    return path


# ── Timeline mapping ─────────────────────────────────────────────────────────
#
# The master timeline is the video's, so these must agree with Cinema's
# `video_time = song_time + offset` — the same relation VisualizerWindow._video_pos
# and cinema_offset_window.video_time_to_x are built on.

def test_positive_offset_puts_the_master_clock_ahead_of_the_song():
    # "+20 ms starts the video earlier", so at song time 10 the video is further
    # into its own timeline than the song is into hers.
    assert master_from_song(10.0, 500) == pytest.approx(10.5)


def test_negative_offset_puts_the_master_clock_behind():
    assert master_from_song(10.0, -500) == pytest.approx(9.5)


def test_master_and_song_conversions_are_inverses():
    for offset in (-34900, -250, 0, 20, 34900):
        assert song_from_master(master_from_song(7.5, offset), offset) == pytest.approx(7.5)


def test_master_mapping_matches_the_editors_own_geometry():
    """Guards against this module and the strip renderer disagreeing on sign.

    ``video_time_to_x`` places video time on the song axis; ``master_from_song``
    goes the other way. Composing them has to be the identity, or the waveform
    the user drags and the audio they hear would move in opposite directions.
    """
    from libraries.cinema_offset_window import song_time_to_x, video_time_to_x
    offset, view_start, pps = 750, 3.0, 90.0
    song_t = 12.0
    master = master_from_song(song_t, offset)
    assert video_time_to_x(master, offset, view_start, pps) == pytest.approx(
        song_time_to_x(song_t, view_start, pps)
    )


# ── Splitting the offset between the bake and audio-delay ────────────────────

def test_bake_and_residual_always_sum_back_to_the_offset():
    # The load-bearing invariant: whatever the bucket does, the two halves must
    # reconstruct the offset exactly or the preview is silently wrong.
    for offset in (-34900, -1234, -501, -500, -499, 0, 19, 20, 499, 500, 501, 34900):
        baked = bake_shift_ms(offset, BAKE_BUCKET_MS)
        assert baked + residual_delay_ms(offset, baked) == offset


def test_residual_stays_within_half_a_bucket_after_baking():
    for offset in range(-3000, 3001, 7):
        baked = bake_shift_ms(offset, BAKE_BUCKET_MS)
        assert abs(residual_delay_ms(offset, baked)) <= BAKE_BUCKET_MS / 2


def test_a_huge_offset_is_absorbed_by_the_bake_not_by_audio_delay():
    """The objection that ruled out a single instance in the first place.

    Cinema's bundled config for "Crab Rave" ships +34900 ms. mpv's audio-delay
    would have to buffer 35 seconds ahead of the playback position; baking
    leaves it a fraction of a second.
    """
    baked = bake_shift_ms(34_900, BAKE_BUCKET_MS)
    assert baked == 35_000
    assert abs(residual_delay_ms(34_900, baked)) <= BAKE_BUCKET_MS / 2


# ── Shifting the PCM ─────────────────────────────────────────────────────────

def test_a_positive_shift_prepends_exactly_that_much_silence():
    pcm = b"\x01\x02" * 1000
    silence, body = wav_bytes_for_shift(pcm, 100)
    assert len(silence) == 100 * BYTES_PER_MS
    assert set(silence) == {0}
    assert body == pcm


def test_a_negative_shift_trims_the_head_instead():
    pcm = bytes(range(256)) * 400
    silence, body = wav_bytes_for_shift(pcm, -10)
    assert silence == b""
    assert body == pcm[10 * BYTES_PER_MS:]


def test_no_shift_leaves_the_audio_untouched():
    pcm = b"\xaa\xbb" * 500
    assert wav_bytes_for_shift(pcm, 0) == (b"", pcm)


def test_the_millisecond_grid_lands_on_whole_samples():
    """192 bytes per ms is why the shift never needs rounding.

    Any other sample rate would leave a remainder and put the two channels out
    of step by a sample, which is a click on every rebake.
    """
    assert BYTES_PER_MS == SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH // 1000
    assert BYTES_PER_MS % (CHANNELS * SAMPLE_WIDTH) == 0


def test_trimming_past_the_end_yields_no_audio_rather_than_an_error():
    # A large negative offset moves the whole song outside the video's timeline;
    # that's a legitimate state, not a failure.
    pcm = b"\x00" * (10 * BYTES_PER_MS)
    silence, body = wav_bytes_for_shift(pcm, -5000)
    assert silence == b""
    assert body == b""


def test_a_thirty_five_second_pad_is_a_few_megabytes():
    """Sanity on the claim that baking a huge offset is cheap."""
    silence, _ = wav_bytes_for_shift(b"", 34_900)
    assert len(silence) < 8 * 1024 * 1024


# ── Ear mixing ───────────────────────────────────────────────────────────────


def test_the_ear_assignment_matches_the_shipping_preview():
    """The graph and the current per-player `af` must pan identically.

    Only the wrapper and the sample-rate clamp differ; if the pan expressions
    ever diverge, the A/B stops comparing like with like.
    """
    for gains in ((1.0, _EAR_BLEED), (_EAR_BLEED, 1.0)):
        chain_pan = pan_chain(*gains).split("pan=", 1)[1]
        filter_pan = pan_filter(*gains).split("pan=", 1)[1].removesuffix("]")
        assert chain_pan == filter_pan


def test_gains_are_halved_so_the_mix_cannot_clip():
    # Nothing downstream will rescue a clip; the halving is the headroom.
    assert "c0=0.5000*c0+0.5000*c1" in pan_chain(1.0, 0.0)


def test_the_channel_layout_is_normalised_before_panning():
    # pan addresses channels positionally: a mono song has no c1 and would fail
    # the graph outright.
    chain = pan_chain(1.0, 0.1)
    assert chain.index("aformat=") < chain.index("pan=")
    assert "channel_layouts=stereo" in chain


def test_both_chains_are_resampled_to_a_common_rate():
    # Both sources are decoded to one rate before they are ever summed.
    assert f"sample_rates={SAMPLE_RATE}" in pan_chain(1.0, 0.1)


def _samples(pcm: bytes):
    import array
    a = array.array("h")
    a.frombytes(pcm)
    return a


def test_the_video_goes_to_the_left_ear_and_the_song_to_the_right():
    """Cinema's README tells users the video is on the left; reversing this
    would invert advice they already have."""
    loud = b"\x00\x40" * 200          # 16384 in both channels
    quiet = b"\x00\x00" * 200
    mixed = _samples(mix_preview(quiet, loud, 0, split=True))
    assert abs(mixed[0]) > abs(mixed[1])        # video-only -> left dominates
    mixed = _samples(mix_preview(loud, quiet, 0, split=True))
    assert abs(mixed[1]) > abs(mixed[0])        # song-only -> right dominates


def test_each_ear_keeps_a_deliberate_bleed_of_the_other():
    # Hard separation is fatiguing; Cinema pans to 0.9 rather than 1.0 for the
    # same reason. The quiet ear must not be silent.
    loud = b"\x00\x40" * 200
    mixed = _samples(mix_preview(b"\x00\x00" * 200, loud, 0, split=True))
    assert mixed[1] != 0
    assert abs(mixed[1]) < abs(mixed[0]) / 2


def test_the_song_is_shifted_inside_the_mix():
    """The shift and the ear routing have to happen in one buffer, or they can
    disagree — which is exactly what an mpv filter graph let happen."""
    song = b"\x00\x40" * (10 * BYTES_PER_MS // 2)
    video = b"\x00\x00" * (10 * BYTES_PER_MS // 2)
    mixed = _samples(mix_preview(song, video, 5, split=True))
    # 5 ms of silence first, then the song appears in the right ear.
    lead = 5 * BYTES_PER_MS // 2
    assert set(mixed[:lead]) == {0}
    assert any(v != 0 for v in mixed[lead:lead + 100])


def test_an_unsplit_mix_is_just_the_shifted_song():
    song = b"\x11\x22" * 500
    assert mix_preview(song, b"\x33\x44" * 500, 0, split=False) == song


def test_a_video_without_audio_falls_back_to_the_song_alone():
    song = b"\x11\x22" * 500
    assert mix_preview(song, None, 0, split=True) == song


def test_a_short_video_does_not_truncate_the_song():
    """Cinema videos are routinely shorter than the map's audio."""
    song = b"\x00\x40" * 1000
    video = b"\x00\x40" * 100
    mixed = mix_preview(song, video, 0, split=True)
    assert len(mixed) == len(song)
    assert any(v != 0 for v in _samples(mixed)[-50:])   # song still audible at the end


@pytest.mark.skipif(_NO_FFMPEG, reason="ffmpeg not installed")
def test_the_in_process_pan_matches_ffmpegs_sample_for_sample(tmp_path):
    """The mix moved out of ffmpeg and into this module; it has to sound identical.

    ``pan_chain`` is still the written definition of the ear gains, so the
    reimplementation is held to it by running both over the same samples rather
    than by reading the two and hoping.
    """
    src = tmp_path / "a.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration=0.5:sample_rate={SAMPLE_RATE}",
         "-ac", "2", "-c:a", "pcm_s16le", str(src), "-y"],
        check=True, capture_output=True,
    )
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(src), "-f", "s16le",
         "-ar", str(SAMPLE_RATE), "-ac", "2", "-"],
        check=True, capture_output=True,
    ).stdout
    through_ffmpeg = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(src),
         "-af", pan_chain(1.0, _EAR_BLEED), "-f", "s16le",
         "-ar", str(SAMPLE_RATE), "-ac", "2", "-"],
        check=True, capture_output=True,
    ).stdout

    ours = _samples(pan_ears(raw, 1.0, _EAR_BLEED))
    theirs = _samples(through_ffmpeg)
    assert len(ours) == len(theirs)
    worst = max(abs(a - b) for a, b in zip(ours, theirs))
    # Rounding differs between the two implementations; a couple of LSBs is
    # inaudible, a systematic gain error would not be.
    assert worst <= 2, f"worst sample difference {worst}"


@pytest.mark.skipif(_NO_FFMPEG, reason="ffmpeg not installed")
def test_the_two_ears_end_up_aligned_to_the_sample(tmp_path):
    """The bug this design replaces, measured the way it was diagnosed.

    A capture of the filter-graph version showed the video ear a constant
    848 ms behind the song ear. Cross-correlating the mix this module builds
    has to put the peak at zero.
    """
    import array
    lead_s, tone_s = 0.30, 0.50
    # Same music in both, but the "song" copy starts later — exactly what a
    # Cinema offset describes.
    song = _lead_in(tmp_path / "song.ogg", lead_s, tone_s=tone_s)
    video = _lead_in(tmp_path / "video.ogg", 0.0, tone_s=tone_s)
    song_pcm = PcmSource.decode(song).pcm
    video_pcm = PcmSource.decode(video).pcm

    # offset = video_onset - song_onset = -lead, so the shift removes the lead.
    mixed = _samples(mix_preview(song_pcm, video_pcm,
                                 int(-lead_s * 1000), split=True))
    left = array.array("h", mixed[0::2])
    right = array.array("h", mixed[1::2])

    def corr_at(lag):
        n = min(len(left), len(right)) - abs(lag)
        a = left[max(0, lag):max(0, lag) + n]
        b = right[max(0, -lag):max(0, -lag) + n]
        return sum(x * y for x, y in zip(a, b))

    step = SAMPLE_RATE // 1000          # 1 ms
    best = max(range(-40 * step, 40 * step + 1, step), key=corr_at)
    assert abs(best) <= step, f"ears misaligned by {best / step:.0f} ms"


# ── Track discovery ──────────────────────────────────────────────────────────

def test_the_external_track_is_the_song():
    tracks = AudioTracks.from_track_list([
        {"id": 1, "type": "video"},
        {"id": 1, "type": "audio", "external": False},
        {"id": 2, "type": "audio", "external": True},
    ])
    assert (tracks.internal, tracks.external) == (1, 2)
    assert tracks.can_split


def test_a_silent_video_leaves_the_song_at_aid1():
    """The case that makes hardcoding [aid2] a bug rather than a shortcut."""
    tracks = AudioTracks.from_track_list([
        {"id": 1, "type": "video"},
        {"id": 1, "type": "audio", "external": True},
    ])
    assert tracks.external == 1
    assert tracks.internal is None
    assert not tracks.can_split  # nothing to put in the other ear


def test_malformed_track_entries_are_skipped_not_fatal():
    tracks = AudioTracks.from_track_list([
        {"type": "audio"},                      # no id
        {"id": "x", "type": "audio"},           # unparseable id
        None,
        {"id": 4, "type": "audio", "external": True},
    ])
    assert tracks.external == 4


def test_an_empty_track_list_can_split_nothing():
    assert not AudioTracks.from_track_list([]).can_split
    assert not AudioTracks.from_track_list(None).can_split


def test_a_build_that_omits_the_external_flag_still_works():
    """The regression: some libmpv builds report only external-filename.

    Reading that as "not external" makes the added song look like the video's
    own track and leaves nothing to route, which surfaces as "the song track
    did not attach" on a video that plays perfectly well.
    """
    tracks = AudioTracks.from_track_list([
        {"id": 1, "type": "audio"},
        {"id": 2, "type": "audio", "external-filename": "/tmp/song-1.wav"},
    ])
    assert (tracks.internal, tracks.external) == (1, 2)


def test_an_observed_added_id_beats_whatever_the_flags_say():
    """Diffing ids is the version-independent identification, so it wins."""
    tracks = AudioTracks.from_track_list(
        [{"id": 1, "type": "audio"}, {"id": 2, "type": "audio"}],
        external_id=2,
    )
    assert (tracks.internal, tracks.external) == (1, 2)


def test_audio_ids_ignore_video_and_subtitle_tracks():
    listing = [
        {"id": 1, "type": "video"},
        {"id": 1, "type": "audio"},
        {"id": 2, "type": "audio"},
        {"id": 1, "type": "sub"},
    ]
    assert audio_track_ids(listing) == {1, 2}


def test_audio_ids_survive_a_malformed_entry():
    assert audio_track_ids([{"id": "x", "type": "audio"}, {"id": 3, "type": "audio"}]) == {3}


def test_the_track_summary_names_what_mpv_reported():
    # This string ends up in the editor's status line, so it has to be legible.
    summary = describe_tracks([
        {"id": 1, "type": "audio", "codec": "aac"},
        {"id": 2, "type": "audio", "codec": "pcm_s16le", "external": True},
    ])
    assert "#1" in summary and "aac" in summary
    assert "#2 external" in summary


def test_the_summary_says_so_when_there_is_no_audio_at_all():
    assert "no audio tracks" in describe_tracks([{"id": 1, "type": "video"}])


# ── Waiting for the added track ──────────────────────────────────────────────

class _FakePlayer:
    """A track list that only reveals the added track after a few polls.

    Models python-mpv's namespaces the way the real binding does: ``__getitem__``
    is *option* access and raises for a property name, so a test double that
    answered both would have hidden the bug this class now guards.
    """

    def __init__(self, listings):
        self._listings = list(listings)
        self.polls = 0

    def _get_property(self, name):
        if name != "track-list":
            raise AttributeError(f"mpv property does not exist: {name}")
        self.polls += 1
        return self._listings[min(self.polls - 1, len(self._listings) - 1)]

    def __getitem__(self, name):
        # What python-mpv really does: prefix with options/ and look that up.
        raise AttributeError(f"mpv property does not exist: options/{name}")


def test_the_added_track_is_awaited_rather_than_read_immediately():
    """audio-add returns before track-list has necessarily caught up.

    Reading straight afterwards is the race that produced the bug report: a
    perfectly good video, and a song track that had simply not been published
    yet.
    """
    from libraries.cinema_preview import SinglePlayerPreview
    before = {1}
    player = _FakePlayer([
        [{"id": 1, "type": "audio"}],                              # not yet
        [{"id": 1, "type": "audio"}],                              # still not
        [{"id": 1, "type": "audio"}, {"id": 2, "type": "audio"}],  # there
    ])
    added, tracks = SinglePlayerPreview._await_added_track(player, before, timeout_s=2)
    assert added == 2
    assert len(tracks) == 2


def test_a_track_that_never_appears_raises_so_the_fallback_can_run():
    """The other half of the bug: a failed attach left a dead preview.

    It has to raise, because PreviewError is what start_preview catches to drop
    back to the two-instance engine.
    """
    from libraries.cinema_preview import SinglePlayerPreview
    player = _FakePlayer([[{"id": 1, "type": "audio", "codec": "aac"}]])
    with pytest.raises(PreviewError) as excinfo:
        SinglePlayerPreview._await_added_track(player, {1}, timeout_s=0.2)
    # The message has to carry what mpv actually reported, or the next report
    # of this is just as opaque as the first.
    assert "#1" in str(excinfo.value) and "aac" in str(excinfo.value)


def test_the_lowest_new_id_is_taken_when_several_appear():
    from libraries.cinema_preview import SinglePlayerPreview
    player = _FakePlayer([[{"id": 1, "type": "audio"},
                           {"id": 2, "type": "audio"},
                           {"id": 3, "type": "audio"}]])
    added, _ = SinglePlayerPreview._await_added_track(player, {1}, timeout_s=1)
    assert added == 2


# ── ffmpeg-backed ────────────────────────────────────────────────────────────


@pytest.mark.skipif(_NO_FFMPEG, reason="ffmpeg not installed")
def test_decoding_yields_the_expected_number_of_samples(tmp_path):
    pcm = PcmSource.decode(_sine(tmp_path / "a.ogg", seconds=2.0))
    assert pcm.duration_s == pytest.approx(2.0, abs=0.05)


@pytest.mark.skipif(_NO_FFMPEG, reason="ffmpeg not installed")
def test_a_mono_song_is_decoded_to_stereo(tmp_path):
    """Beat Saber ships plenty of mono audio; the shift math assumes two channels."""
    pcm = PcmSource.decode(_sine(tmp_path / "a.ogg", seconds=1.0, channels=1))
    assert pcm.duration_s == pytest.approx(1.0, abs=0.05)


@pytest.mark.skipif(_NO_FFMPEG, reason="ffmpeg not installed")
def test_a_baked_wav_is_longer_by_exactly_the_shift(tmp_path):
    pcm = PcmSource.decode(_sine(tmp_path / "a.ogg", seconds=1.0))
    out = pcm.write_shifted_wav(tmp_path / "shifted.wav", 250)
    with wave.open(str(out), "rb") as wav:
        assert wav.getframerate() == SAMPLE_RATE
        assert wav.getnchannels() == CHANNELS
        shifted_s = wav.getnframes() / wav.getframerate()
    assert shifted_s == pytest.approx(pcm.duration_s + 0.25, abs=0.01)


@pytest.mark.skipif(_NO_FFMPEG, reason="ffmpeg not installed")
def test_a_baked_wav_starts_silent_for_exactly_the_shift(tmp_path):
    """The lead-in has to be real silence, or Detect Silence would find it."""
    pcm = PcmSource.decode(_sine(tmp_path / "a.ogg", seconds=1.0))
    out = pcm.write_shifted_wav(tmp_path / "shifted.wav", 200)
    with wave.open(str(out), "rb") as wav:
        head = wav.readframes(int(SAMPLE_RATE * 0.2))
    assert set(head) == {0}


@pytest.mark.skipif(_NO_FFMPEG, reason="ffmpeg not installed")
def test_a_negative_shift_removes_audio_from_the_front(tmp_path):
    pcm = PcmSource.decode(_sine(tmp_path / "a.ogg", seconds=1.0))
    out = pcm.write_shifted_wav(tmp_path / "shifted.wav", -300)
    with wave.open(str(out), "rb") as wav:
        shifted_s = wav.getnframes() / wav.getframerate()
    assert shifted_s == pytest.approx(pcm.duration_s - 0.3, abs=0.01)


# ── End to end ───────────────────────────────────────────────────────────────


@pytest.mark.skipif(_NO_FFMPEG, reason="ffmpeg not installed")
@pytest.mark.parametrize("song_lead_s, video_lead_s", [
    (0.5, 2.0),    # offset lands exactly on a bucket: residual 0
    (0.5, 2.12),   # offset between buckets: the residual carries the remainder
    (0.3, 0.05),   # negative offset: the song's head is trimmed instead
])
def test_the_baked_song_lands_on_the_videos_timeline(tmp_path, song_lead_s, video_lead_s):
    """The whole design, checked against ffmpeg rather than against itself.

    Two files whose music starts at different points. ``detected_offset_ms``
    says what offset lines them up; the bake plus mpv's residual ``audio-delay``
    must put the song's first sound at the same master-timeline moment as the
    video's. If these two ever disagree the preview is silently wrong, and no
    amount of drift correction would show it — both clocks would be perfectly
    locked to the wrong relationship.
    """
    from libraries.audio_utils import first_sound_offset_s
    from libraries.cinema_offset_window import detected_offset_ms

    song = _lead_in(tmp_path / "song.ogg", song_lead_s)
    video = _lead_in(tmp_path / "video.mp4", video_lead_s,
                     container="mp4", codec="aac")

    song_onset = first_sound_offset_s(song)
    video_onset = first_sound_offset_s(video)
    offset_ms = detected_offset_ms(song_onset, video_onset)

    baked = bake_shift_ms(offset_ms)
    residual_s = residual_delay_ms(offset_ms, baked) / 1000.0

    shifted = PcmSource.decode(song).write_shifted_wav(tmp_path / "shifted.wav", baked)
    baked_onset = first_sound_offset_s(shifted)

    # Where the song's first sound is actually heard: the bake put it at
    # baked_onset on the master timeline, and audio-delay moves it by the rest.
    assert baked_onset + residual_s == pytest.approx(video_onset, abs=0.03)


@pytest.mark.skipif(_NO_FFMPEG, reason="ffmpeg not installed")
def test_a_crab_rave_sized_offset_bakes_without_a_large_residual(tmp_path):
    """+34900 ms end to end — the case that ruled out a single instance."""
    song = _lead_in(tmp_path / "song.ogg", 0.2, tone_s=0.5)
    offset_ms = 34_900
    baked = bake_shift_ms(offset_ms)
    residual_s = residual_delay_ms(offset_ms, baked) / 1000.0

    shifted = PcmSource.decode(song).write_shifted_wav(tmp_path / "shifted.wav", baked)
    from libraries.audio_utils import first_sound_offset_s
    baked_onset = first_sound_offset_s(shifted)

    assert baked_onset + residual_s == pytest.approx(0.2 + offset_ms / 1000.0, abs=0.03)
    # And mpv is never asked for more than half a bucket of delay.
    assert abs(residual_s) <= BAKE_BUCKET_MS / 2000.0


@pytest.mark.skipif(_NO_FFMPEG, reason="ffmpeg not installed")
def test_a_shift_that_trims_everything_leaves_an_empty_wav(tmp_path):
    """Recognisable as header-only, so start() can name the real cause.

    Without the guard mpv rejects the empty file and it surfaces as a missing
    track — the same opaque message as the attach race, for a completely
    different reason.
    """
    from libraries.cinema_preview import _WAV_HEADER_BYTES
    pcm = PcmSource.decode(_sine(tmp_path / "a.ogg", seconds=0.5))
    out = pcm.write_shifted_wav(tmp_path / "gone.wav", -5000)
    assert out.stat().st_size <= _WAV_HEADER_BYTES


def test_decoding_a_missing_file_raises_preview_error(tmp_path):
    with pytest.raises(PreviewError):
        PcmSource.decode(tmp_path / "nope.ogg")


@pytest.mark.skipif(_NO_FFMPEG, reason="ffmpeg not installed")
def test_decoding_without_ffmpeg_raises_preview_error(tmp_path, monkeypatch):
    src = _sine(tmp_path / "a.ogg", seconds=0.2)
    monkeypatch.setattr("libraries.cinema_preview.find_ffmpeg", lambda: None)
    with pytest.raises(PreviewError):
        PcmSource.decode(src)


# ── Engine preference ────────────────────────────────────────────────────────

@pytest.fixture
def config(monkeypatch):
    """An in-memory app config, so these never touch the real one."""
    from libraries import app_config
    store: dict = {}
    monkeypatch.setattr(app_config, "load_config", lambda: dict(store))
    monkeypatch.setattr(app_config, "save_config",
                        lambda cfg: (store.clear(), store.update(cfg), True)[2])
    return store


def test_the_single_instance_engine_is_the_default(config):
    from libraries import app_config
    assert app_config.get_preview_engine() == "single"


def test_the_engine_choice_round_trips(config):
    from libraries import app_config
    assert app_config.set_preview_engine("two-instance")
    assert app_config.get_preview_engine() == "two-instance"


def test_an_unknown_engine_name_is_refused_not_stored(config):
    from libraries import app_config
    assert not app_config.set_preview_engine("quadraphonic")
    assert "cinema_preview_engine" not in config


def test_a_corrupted_engine_value_falls_back_to_the_default(config):
    # A hand-edited config shouldn't be able to break the preview entirely.
    from libraries import app_config
    config["cinema_preview_engine"] = 17
    assert app_config.get_preview_engine() == "single"


# ── Fallback ─────────────────────────────────────────────────────────────────
#
# start_preview is the only place that decides which engine runs, so the
# degradation path is worth pinning even though neither engine can actually be
# constructed in a test environment.

class _FakeEngine:
    """Stands in for a started engine; records what it was asked for."""

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.started = None

    def start(self, at_song_s, offset_ms, *, split=False):
        self.started = (at_song_s, offset_ms, split)


@pytest.fixture
def engines(monkeypatch):
    """Replace both engines with fakes and report which one got built."""
    built: list[str] = []

    def _fake(name, fail=False):
        class _E(_FakeEngine):
            def start(self, *a, **k):
                built.append(name)
                if fail:
                    raise PreviewError("libmpv said no")
                super().start(*a, **k)
        return _E

    def _install(single_fails: bool):
        monkeypatch.setattr("libraries.cinema_preview.SinglePlayerPreview",
                            _fake("single", fail=single_fails))
        monkeypatch.setattr("libraries.cinema_preview.TwoInstancePreview",
                            _fake("two-instance"))
        # start_preview decodes the video's audio too, for the ear mix, and
        # reads .pcm off the result — so the stand-in has to carry one.
        monkeypatch.setattr("libraries.cinema_preview.PcmSource.decode",
                            staticmethod(lambda *a, **k: PcmSource(b"\0" * 64, "x")))
        return built

    return _install


def test_the_preferred_engine_is_used_when_it_works(engines, tmp_path, config):
    from libraries.cinema_preview import start_preview
    built = engines(single_fails=False)
    engine, note = start_preview(tmp_path / "a.ogg", tmp_path / "v.mp4", 5.0, 250,
                                 prefer="single")
    assert built == ["single"]
    assert note == ""
    assert engine.started == (5.0, 250, False)


def test_a_failing_single_instance_falls_back_to_two(engines, tmp_path, config):
    """The behaviour the default rests on: degrade to a working preview.

    Same shape as VisualizerWindow._start_stream dropping from video to
    spectrum — a libmpv that won't take an external track or a filter graph
    should cost sync quality, not the whole feature.
    """
    from libraries.cinema_preview import start_preview
    built = engines(single_fails=True)
    engine, note = start_preview(tmp_path / "a.ogg", tmp_path / "v.mp4", 1.0, 0,
                                 prefer="single")
    assert built == ["single", "two-instance"]
    assert engine.started == (1.0, 0, False)
    assert "libmpv said no" in note and "drift" in note.lower()


def test_asking_for_two_instances_does_not_try_the_other_first(engines, tmp_path, config):
    # Pinning the config to the old engine has to actually skip the decode,
    # which is the expensive part.
    from libraries.cinema_preview import start_preview
    built = engines(single_fails=False)
    _, note = start_preview(tmp_path / "a.ogg", tmp_path / "v.mp4", 0.0, 0,
                            prefer="two-instance")
    assert built == ["two-instance"]
    assert note == ""


def test_the_config_chooses_when_no_preference_is_passed(engines, tmp_path, config):
    from libraries import app_config
    from libraries.cinema_preview import start_preview
    built = engines(single_fails=False)
    app_config.set_preview_engine("two-instance")
    start_preview(tmp_path / "a.ogg", tmp_path / "v.mp4", 0.0, 0)
    assert built == ["two-instance"]


def test_both_engines_failing_raises_rather_than_returning_nothing(monkeypatch, tmp_path, config):
    from libraries import cinema_preview

    class _Dead:
        def __init__(self, *a, **k):
            pass

        def start(self, *a, **k):
            raise PreviewError("nope")

    monkeypatch.setattr(cinema_preview, "SinglePlayerPreview", _Dead)
    monkeypatch.setattr(cinema_preview, "TwoInstancePreview", _Dead)
    monkeypatch.setattr(cinema_preview.PcmSource, "decode",
                        staticmethod(lambda *a, **k: cinema_preview.PcmSource(b"\0" * 64, "x")))
    with pytest.raises(PreviewError):
        cinema_preview.start_preview(tmp_path / "a.ogg", tmp_path / "v.mp4", 0.0, 0,
                                     prefer="single")


def test_the_split_setting_reaches_the_engine(engines, tmp_path, config):
    from libraries.cinema_preview import start_preview
    engines(single_fails=False)
    engine, _ = start_preview(tmp_path / "a.ogg", tmp_path / "v.mp4", 0.0, 0,
                              split=True, prefer="single")
    assert engine.started[2] is True


# ── Property vs option access ────────────────────────────────────────────────
#
# python-mpv's player[name] reads an *option*; it prefixes the lookup with
# "options/". track-list and time-pos exist only as properties, so item access
# fails them with "mpv property does not exist" — which is exactly what the
# preview did on a real libmpv while every test passed.

class _PropOnlyPlayer:
    """Properties readable, options not — the shape that broke."""

    def __init__(self):
        self.props = {"track-list": [{"id": 1, "type": "audio"}], "time-pos": 4.0}
        self.written = {}

    def _get_property(self, name):
        if name not in self.props:
            raise AttributeError(f"mpv property does not exist: {name}")
        return self.props[name]

    def _set_property(self, name, value):
        self.written[name] = value

    def __getitem__(self, name):
        raise AttributeError(f"mpv property does not exist: options/{name}")

    def __setitem__(self, name, value):
        raise AttributeError(f"mpv property does not exist: options/{name}")


class _BoundPropPlayer:
    """A binding that exposes properties as attributes, as python-mpv does."""

    def __init__(self):
        self._pos = 1.0

    @property
    def time_pos(self):
        return self._pos

    @time_pos.setter
    def time_pos(self, value):
        self._pos = value


def test_properties_are_read_without_going_through_option_access():
    from libraries.cinema_preview import prop_get
    player = _PropOnlyPlayer()
    assert prop_get(player, "track-list") == [{"id": 1, "type": "audio"}]
    assert prop_get(player, "time-pos") == 4.0


def test_underscored_and_dashed_names_reach_the_same_property():
    from libraries.cinema_preview import prop_get
    player = _PropOnlyPlayer()
    assert prop_get(player, "time_pos") == prop_get(player, "time-pos")


def test_a_bound_attribute_is_preferred_when_the_binding_offers_one():
    from libraries.cinema_preview import prop_get, prop_set
    player = _BoundPropPlayer()
    assert prop_get(player, "time-pos") == 1.0
    assert prop_set(player, "time-pos", 9.0)
    assert player.time_pos == 9.0


def test_writing_a_property_does_not_go_through_option_access():
    from libraries.cinema_preview import prop_set
    player = _PropOnlyPlayer()
    assert prop_set(player, "audio-delay", 0.25)
    assert player.written == {"audio-delay": 0.25}


def test_an_unknown_property_reports_failure_rather_than_silently_sticking():
    """A plain setattr would create an instance attribute and look successful
    while mpv carried on unchanged — worse than an error, because nothing
    would ever surface it."""
    from libraries.cinema_preview import prop_set

    class _Refuses:
        def _set_property(self, name, value):
            raise AttributeError("no such property")

    player = _Refuses()
    assert not prop_set(player, "lavfi-complex", "[aid1]anull[ao]")
    assert not hasattr(player, "lavfi_complex")


def test_reading_a_missing_property_yields_none_not_an_exception():
    from libraries.cinema_preview import prop_get
    assert prop_get(_PropOnlyPlayer(), "nonexistent") is None
    assert prop_get(None, "time-pos") is None


# ── Exact baking ─────────────────────────────────────────────────────────────
#
# The engine defaults to baking the offset exactly, so audio-delay carries
# nothing in the steady state. That is a correctness decision: a residual is
# only as good as mpv's willingness to honour it, and a build that quietly
# ignores audio-delay produced a preview that was consistently off time while
# every piece of arithmetic above it was right.

def test_an_exact_bake_leaves_no_residual():
    for offset in (-11863, -500, 0, 137, 34_900):
        baked = bake_shift_ms(offset, 0)
        assert baked == offset
        assert residual_delay_ms(offset, baked) == 0


def test_the_engine_bakes_exactly_by_default():
    from libraries.cinema_preview import SinglePlayerPreview
    engine = SinglePlayerPreview.__new__(SinglePlayerPreview)
    engine._bucket_ms = SinglePlayerPreview.__init__.__defaults__ and 0
    # Constructed properly, the bucket is 0 — nothing is left to audio-delay.
    import inspect
    sig = inspect.signature(SinglePlayerPreview.__init__)
    assert sig.parameters["bucket_ms"].default == 0


class _MpvDouble:
    """Enough of python-mpv to drive the bake/swap path."""

    def __init__(self, with_video_audio=True):
        tracks = [{"id": 1, "type": "video"}]
        if with_video_audio:
            tracks.append({"id": 1, "type": "audio", "codec": "aac"})
        self.tracks = tracks
        self.props = {"time-pos": 20.0, "audio-delay": 0.0}
        self.added = []
        self.removed = []
        self.delay_works = True

    def _alloc_id(self) -> int:
        # mpv hands out an unused id; a double that reissued one would make a
        # genuine attach look like a failure to attach.
        used = {t["id"] for t in self.tracks if t.get("type") == "audio"}
        return max(used, default=0) + 1

    # python-mpv property namespace
    def _get_property(self, name):
        if name == "track-list":
            return list(self.tracks)
        if name in self.props:
            return self.props[name]
        raise AttributeError(f"mpv property does not exist: {name}")

    def _set_property(self, name, value):
        if name == "audio-delay" and not self.delay_works:
            return  # accepted and ignored — the build that caused the bug
        self.props[name] = value

    def __getitem__(self, name):
        raise AttributeError(f"mpv property does not exist: options/{name}")

    def command(self, name, *args):
        if name == "audio-add":
            self.added.append(args[0])
            self.tracks.append({"id": self._alloc_id(), "type": "audio",
                                "codec": "pcm_s16le", "external-filename": args[0]})
        elif name == "audio-remove":
            rid = int(args[0])
            self.removed.append(rid)
            self.tracks = [t for t in self.tracks
                           if not (t.get("type") == "audio" and t.get("id") == rid
                                   and t.get("external-filename"))]


def _engine_with(tmp_path, player, offset_ms=0, pcm_seconds=2.0):
    """A started-looking engine wired to a double, without touching libmpv."""
    from libraries.cinema_preview import AudioTracks, SinglePlayerPreview
    pcm = PcmSource(b"\x01\x00\x02\x00" * int(pcm_seconds * SAMPLE_RATE / 2),
                    tmp_path / "song.ogg")
    engine = SinglePlayerPreview(tmp_path / "v.mp4", pcm)
    engine._player = player
    engine._tmpdir = tmp_path
    engine._offset_ms = offset_ms
    engine._baked_shift_ms = offset_ms
    engine._tracks = AudioTracks(1, None)
    return engine


def _settle(engine, timeout=5.0):
    """Wait for the background bake worker to go idle."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with engine._bake_lock:
            if not engine._baking:
                return True
        time.sleep(0.01)
    return False


def test_a_nudge_ends_up_baked_not_left_on_audio_delay(tmp_path):
    """The fix for the report: the offset must land in the audio data.

    audio-delay is written first because it's instant, but the bake is what the
    result rests on — so once it settles there is no residual at all.
    """
    player = _MpvDouble()
    engine = _engine_with(tmp_path, player, offset_ms=0)
    engine.set_offset_ms(137)
    assert _settle(engine)
    assert engine.baked_shift_ms == 137
    assert engine.residual_delay_ms == 0
    assert player.added, "a rebaked WAV should have been attached"


def test_it_stays_correct_when_audio_delay_is_silently_ignored(tmp_path):
    """The build that caused the bug: audio-delay accepted, then ignored.

    Because the offset is baked, the preview still ends up on time — that is
    the whole point of not resting on the delay.
    """
    player = _MpvDouble()
    player.delay_works = False
    engine = _engine_with(tmp_path, player, offset_ms=0)
    engine.set_offset_ms(-11_863)
    assert _settle(engine)
    assert engine.baked_shift_ms == -11_863
    assert engine.residual_delay_ms == 0


def test_a_verified_dead_audio_delay_is_reported(tmp_path):
    player = _MpvDouble()
    player.delay_works = False
    engine = _engine_with(tmp_path, player)
    assert engine._verify_audio_delay() is False
    player.delay_works = True
    assert engine._verify_audio_delay() is True


def test_the_probe_leaves_no_delay_behind(tmp_path):
    # A probe that forgot to reset would itself put the preview 250 ms out.
    player = _MpvDouble()
    engine = _engine_with(tmp_path, player)
    engine._verify_audio_delay()
    assert player.props["audio-delay"] == 0.0


def test_rapid_nudges_coalesce_into_one_bake(tmp_path):
    """Holding an arrow key must not queue a 30 MB write per repeat."""
    player = _MpvDouble()
    engine = _engine_with(tmp_path, player, offset_ms=0)
    for step in range(20, 201, 20):
        engine.set_offset_ms(step)
    assert _settle(engine)
    assert engine.baked_shift_ms == 200
    assert engine.residual_delay_ms == 0
    assert len(player.added) < 10, f"{len(player.added)} bakes for 10 nudges"


def test_a_drag_rides_on_the_delay_and_bakes_on_release(tmp_path):
    """A real drag releases on the value its last motion event already set.

    This test used to release on a *different* value, which is not what the UI
    does — and that gap hid a bug where dragging the waveform never took effect
    until the preview was restarted. Modelling the release faithfully is the
    whole point of the case.
    """
    player = _MpvDouble()
    engine = _engine_with(tmp_path, player, offset_ms=0)

    for step in (100, 200, 300):       # motion events
        engine.set_offset_ms(step, settling=True)
    assert player.added == []          # nothing rebuilt mid-drag
    assert engine.residual_delay_ms == 300

    engine.set_offset_ms(300)          # release: same value as the last motion
    assert _settle(engine)
    assert engine.baked_shift_ms == 300
    assert engine.residual_delay_ms == 0
    assert player.added, "the release must bake what the drag deferred"


def test_a_release_with_nothing_pending_does_not_rebake(tmp_path):
    """The flip side: idempotent, so a redundant settle costs nothing."""
    player = _MpvDouble()
    engine = _engine_with(tmp_path, player, offset_ms=0)
    engine.set_offset_ms(50)
    assert _settle(engine)
    before = len(player.added)
    engine.set_offset_ms(50)           # same value, nothing deferred
    assert _settle(engine)
    assert len(player.added) == before


def test_the_old_track_is_removed_after_the_new_one_attaches(tmp_path):
    """Removing first would drop to the video's own audio — a blip in the
    wrong ear, and in split stereo a moment of the video in both."""
    from libraries.cinema_preview import AudioTracks
    player = _MpvDouble()
    engine = _engine_with(tmp_path, player, offset_ms=0)
    engine._tracks = AudioTracks(1, 2)
    player.tracks.append({"id": 2, "type": "audio", "external-filename": "old.wav"})
    engine.set_offset_ms(50)
    assert _settle(engine)
    assert player.removed == [2]
    assert engine._tracks.external == 3


def test_stopping_mid_bake_abandons_the_swap(tmp_path):
    """A bake landing after teardown must not command a terminated player."""
    player = _MpvDouble()
    engine = _engine_with(tmp_path, player, offset_ms=0)
    with engine._bake_lock:
        engine._bake_gen += 1        # simulate stop() racing the worker
        gen_before = engine._bake_gen - 1
    ok = engine._swap_in(tmp_path / "whatever.wav", 50, gen_before)
    assert ok is False
    assert engine.baked_shift_ms == 0  # unchanged


def test_the_opening_seek_is_exact_not_keyframe_rounded():
    """hr-seek is load-bearing, not a nicety.

    A keyframe-rounded video seek against a sample-exact WAV seek puts the
    picture up to a GOP behind the sound — constant, and indistinguishable by
    ear from a wrong offset. With one clock there is no watchdog to correct it
    afterwards, so the seek itself has to land.
    """
    import inspect
    from libraries import cinema_preview
    src = inspect.getsource(cinema_preview.SinglePlayerPreview.start)
    assert 'hr_seek="yes"' in src


def test_a_seek_that_lands_early_is_measured(tmp_path):
    player = _MpvDouble()
    engine = _engine_with(tmp_path, player)
    player.props["time-pos"] = 13.0        # asked for 14.1, landed on a keyframe
    assert engine._measure_seek_error(14.1) == pytest.approx(-1.1, abs=0.01)


def test_a_seek_that_lands_where_it_was_sent_measures_zero(tmp_path):
    player = _MpvDouble()
    engine = _engine_with(tmp_path, player)
    player.props["time-pos"] = 14.1
    assert abs(engine._measure_seek_error(14.1)) < 1e-6


def test_drift_is_reported_from_mpv_rather_than_asserted_to_be_zero(tmp_path):
    """The old version returned a flat 0.0 on the grounds that one clock cannot
    drift against itself — true, and beside the point: it says nothing about a
    constant offset baked in when the streams were positioned."""
    player = _MpvDouble()
    engine = _engine_with(tmp_path, player)
    player.props["avsync"] = -0.98
    assert engine.measured_drift_s() == pytest.approx(-0.98)


def test_drift_is_none_when_mpv_will_not_say(tmp_path):
    player = _MpvDouble()
    engine = _engine_with(tmp_path, player)
    assert engine.measured_drift_s() is None   # no avsync property on this double


def test_the_diagnostic_line_carries_the_numbers_that_matter(tmp_path):
    player = _MpvDouble()
    engine = _engine_with(tmp_path, player, offset_ms=250)
    engine._seek_error_s = -1.02
    line = engine.diagnostics()
    for expected in ("baked +250ms", "unbaked +0ms", "seek err -1020ms", "split off"):
        assert expected in line, line


def test_the_engine_reports_what_is_baked_and_what_is_not(tmp_path):
    # The harness readout depends on these, and so does telling a stale
    # residual apart from a broken one.
    player = _MpvDouble()
    engine = _engine_with(tmp_path, player, offset_ms=1000)
    engine._offset_ms = 1120
    assert engine.baked_shift_ms == 1000
    assert engine.residual_delay_ms == 120


# ── Stale self-call guard ────────────────────────────────────────────────────
#
# Renaming a method and missing one call site is invisible until the branch
# runs — and the branch that broke was the one that needs libmpv, so no test
# could reach it. This is a source-level check instead: every ``self._x(...)``
# in these modules must name something the class actually defines.
#
# Private names only, because a public one may legitimately come from a base
# class (tkinter's, in the window's case) and the suite stubs tkinter with
# MagicMocks that answer hasattr for anything.

def _undefined_self_calls(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    missing: list[str] = []
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        defined = {n.name for n in ast.walk(cls) if isinstance(n, ast.FunctionDef)}
        # Attributes assigned anywhere in the class count too: an instance
        # attribute holding a callable is legitimate.
        for node in ast.walk(cls):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets = [node.target]
            for t in targets:
                if (isinstance(t, ast.Attribute)
                        and isinstance(t.value, ast.Name) and t.value.id == "self"):
                    defined.add(t.attr)
        for node in ast.walk(cls):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name) and func.value.id == "self"
                    and func.attr.startswith("_")
                    and func.attr not in defined):
                missing.append(f"{path.name}:{node.lineno} {cls.name}.{func.attr}()")
    return missing


@pytest.mark.parametrize("module", ["cinema_preview.py", "cinema_offset_window.py"])
def test_no_calls_to_methods_that_do_not_exist(module):
    root = pathlib.Path(__file__).resolve().parent.parent
    assert _undefined_self_calls(root / "libraries" / module) == []


def test_the_guard_catches_a_renamed_method(tmp_path):
    """Only worth having if it fails on the shape that got through."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "class Engine:\n"
        "    def start(self):\n"
        "        return self._bake(0)\n"
        "    def _write_bake(self, shift):\n"
        "        return shift\n"
    )
    found = _undefined_self_calls(sample)
    assert len(found) == 1 and "Engine._bake()" in found[0]


def test_the_guard_does_not_flag_a_callable_attribute(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "class Engine:\n"
        "    def __init__(self, fn):\n"
        "        self._hook = fn\n"
        "    def go(self):\n"
        "        return self._hook()\n"
    )
    assert _undefined_self_calls(sample) == []


# ── Names the window and its tests still reach for ───────────────────────────

def test_the_moved_constants_are_still_importable_from_the_window():
    """pan_filter and the drift constants moved to cinema_preview when the
    preview grew a second engine. They are Cinema-behaviour facts that
    test_cinema_offset.py has always imported from the window, so the
    re-exports have to keep resolving to the same objects."""
    from libraries import cinema_offset_window as w
    from libraries import cinema_preview as p
    assert w.pan_filter is p.pan_filter
    assert w.drift_speed_trim is p.drift_speed_trim
    assert w._EAR_BLEED == p.EAR_BLEED
    assert w._DRIFT_TOLERANCE_SPLIT_S == p.DRIFT_TOLERANCE_SPLIT_S


def test_the_window_no_longer_drives_players_itself():
    """The editor delegates to an engine now; the old inline mpv handling is
    gone rather than merely unused, so a stale call site can't linger."""
    from libraries import cinema_offset_window as w
    for gone in ("_check_video_drift", "_set_video_speed", "_seek_video",
                 "_resync_preview_video", "_apply_stereo_split", "_teardown_players"):
        assert not hasattr(w.CinemaOffsetWindow, gone), gone
