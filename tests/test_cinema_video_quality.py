"""What ``cinema_video_quality`` actually makes yt-dlp download.

The unit tests in test_app_config.py assert the *shape* of the arguments. They
cannot catch the thing that matters here, which is a claim about someone else's
software: that ``-S res,vcodec:avc1`` breaks a 1080p tie toward H.264 and takes
VP9 at 4K anyway. Get that backwards and nothing fails — you just quietly
transcode every download, or quietly ship a file Cinema can't play.

So these tests run the real selection engine. yt-dlp is not a dependency of the
app (it shells out to a downloaded binary), so they skip where it isn't
importable; the format tables below are the itags YouTube actually serves,
which is what makes the exercise worth anything.

The re-encode question reduces to the merged container. ``--recode-video mp4``
re-encodes rather than remuxes, but skips outright when the file is already
.mp4 — so "merged as .mp4" *is* "no re-encode", and .mkv/.webm is a transcode.
"""

from __future__ import annotations

import shutil

import pytest

from libraries import app_config

yt_dlp = pytest.importorskip("yt_dlp", reason="selection engine under test")


def _video(format_id, height, vcodec, ext, fps=30):
    return {"format_id": format_id, "url": f"http://test/{format_id}",
            "ext": ext, "height": height, "width": (height * 16) // 9,
            "vcodec": vcodec, "acodec": "none", "tbr": height * 2, "fps": fps,
            "protocol": "https", "filesize": height * 1000}


def _h264_level(width, height, fps):
    """The H.264 level a transcode of this stream lands in (ITU-T H.264 A-1).

    Level is set by macroblocks per second, so frame rate matters as much as
    resolution — which is the entire reason "max" filters on fps.
    """
    macroblocks = (width // 16) * (height // 16)
    rate = macroblocks * fps
    for name, (max_mbps, max_fs) in (("4.2", (522240, 8704)), ("5", (589824, 22080)),
                                     ("5.1", (983040, 36864)), ("5.2", (2073600, 36864))):
        if macroblocks <= max_fs and rate <= max_mbps:
            return name
    return "above-5.2"


# The Media Foundation H.264 decoder documents Baseline/Main/High "up to level
# 5.1". Anything past that is a file Beat Saber may simply refuse to open.
MEDIA_FOUNDATION_MAX_LEVEL = ("4.2", "5", "5.1")


def _audio(format_id, acodec, ext, tbr):
    return {"format_id": format_id, "url": f"http://test/{format_id}",
            "ext": ext, "vcodec": "none", "acodec": acodec, "tbr": tbr,
            "protocol": "https"}


# m4a and Opus, both always present on YouTube. Opus is the higher bitrate of
# the two, so anything that merely sorts audio by quality picks it — and pairs
# an H.264 video into an .mkv, forcing a transcode of a stream that needed
# none. That is the trap ``bestaudio[acodec*=mp4]`` exists to avoid.
_AUDIO = [_audio("140", "mp4a.40.2", "m4a", 128),
          _audio("251", "opus", "webm", 160)]

# A run-of-the-mill upload: H.264, VP9 and AV1, all topping out at 1080p.
TOPS_OUT_AT_1080 = [
    _video("136", 720, "avc1.4d401f", "mp4"),
    _video("137", 1080, "avc1.640028", "mp4"),
    _video("247", 720, "vp9", "webm"),
    _video("248", 1080, "vp9", "webm"),
    _video("398", 720, "av01.0.05M.08", "mp4"),
    _video("399", 1080, "av01.0.08M.08", "mp4"),
] + _AUDIO

# The same upload in 4K. Note what is and isn't there above 1080p: VP9 and AV1
# only. YouTube publishes no H.264 at 1440p or 2160p, which is the whole reason
# "max" has to be willing to re-encode.
HAS_4K = [
    _video("137", 1080, "avc1.640028", "mp4"),
    _video("248", 1080, "vp9", "webm"),
    _video("271", 1440, "vp9", "webm"),
    _video("313", 2160, "vp9", "webm"),
    _video("399", 1080, "av01.0.08M.08", "mp4"),
    _video("401", 2160, "av01.0.12M.08", "mp4"),
] + _AUDIO

# An old or low-effort upload that never got a high-resolution encode.
TOPS_OUT_AT_480 = [
    _video("135", 480, "avc1.4d401e", "mp4"),
    _video("244", 480, "vp9", "webm"),
] + _AUDIO

# A 60fps 4K upload. YouTube publishes every rung at 60fps for these — there
# is no 2160p30 to quietly fall back to, which is what makes this the case
# that would otherwise transcode to level 5.2 and stop playing in-game.
HAS_4K60 = [
    _video("299", 1080, "avc1.64002a", "mp4", fps=60),
    _video("303", 1080, "vp9", "webm", fps=60),
    _video("308", 1440, "vp9", "webm", fps=60),
    _video("315", 2160, "vp9", "webm", fps=60),
    _video("401", 2160, "av01.0.12M.08", "mp4", fps=60),
] + _AUDIO


def _select(quality, formats):
    """Run the real selector and report (video format, merged container)."""
    args = app_config.video_download_args(quality)
    opts = {"format": args[1], "quiet": True, "simulate": True,
            "no_warnings": True}
    if "-S" in args:
        opts["format_sort"] = args[args.index("-S") + 1].split(",")
    info = {"id": "t", "title": "t", "webpage_url": "http://test",
            "extractor": "youtube", "extractor_key": "Youtube",
            "formats": [dict(f) for f in formats]}
    result = yt_dlp.YoutubeDL(opts).process_ie_result(info, download=False)
    chosen = result.get("requested_formats") or [result]
    return chosen[0], result.get("ext")


def _reencodes(container: str) -> bool:
    return container != "mp4"


# ── The behaviour the setting promises ───────────────────────────────────────


def test_max_takes_h264_when_1080p_is_all_there_is():
    """Nothing above 1080p to gain, so there is nothing to re-encode for."""
    video, container = _select("max", TOPS_OUT_AT_1080)
    assert video["height"] == 1080
    assert "avc1" in video["vcodec"]
    assert not _reencodes(container)


def test_max_takes_4k_and_pays_the_re_encode():
    """Above 1080p the only streams are VP9/AV1, so the transcode buys the
    resolution rather than being pure waste."""
    video, container = _select("max", HAS_4K)
    assert video["height"] == 2160
    assert _reencodes(container)


def test_max_takes_vp9_not_av1_at_4k():
    """Both are 2160p. AV1 arrives in an .mp4, so --recode-video would skip it
    and leave Cinema a codec it cannot decode; VP9 arrives in .webm and gets
    converted. Same picture, and only one of them plays."""
    video, _ = _select("max", HAS_4K)
    assert "vp9" in video["vcodec"]
    assert "av01" not in video["vcodec"]


def test_max_stops_at_1440p_on_a_60fps_upload():
    """4K60 transcodes to level 5.2, past what Media Foundation documents.
    1440p60 is the most resolution that still fits inside 5.1."""
    video, _ = _select("max", HAS_4K60)
    assert video["height"] == 1440
    assert video["fps"] == 60


@pytest.mark.parametrize("formats", [TOPS_OUT_AT_1080, HAS_4K, HAS_4K60, TOPS_OUT_AT_480],
                         ids=["1080p-max", "4k30", "4k60", "480p-max"])
def test_max_never_picks_a_stream_that_transcodes_past_level_5_1(formats):
    """The guard the whole fps filter exists for. A file above 5.1 costs a
    long encode and then may not open at all."""
    video, _ = _select("max", formats)
    level = _h264_level(video["width"], video["height"], video["fps"])
    assert level in MEDIA_FOUNDATION_MAX_LEVEL, (
        f"{video['height']}p{video['fps']} transcodes to level {level}")


def test_the_4k30_case_really_is_the_tight_one():
    """Guards the reasoning, not the code: if 4K30 were comfortably inside
    5.1 the fps filter would look like paranoia. It is inside by 1.2%."""
    assert _h264_level(3840, 2160, 30) == "5.1"
    assert _h264_level(3840, 2160, 60) == "5.2"


def test_max_does_not_re_encode_a_low_resolution_upload():
    video, container = _select("max", TOPS_OUT_AT_480)
    assert video["height"] == 480
    assert "avc1" in video["vcodec"]
    assert not _reencodes(container)


# ── The settings that must never re-encode ───────────────────────────────────


@pytest.mark.parametrize("formats", [TOPS_OUT_AT_1080, HAS_4K, TOPS_OUT_AT_480],
                         ids=["1080p-max", "4k", "480p-max"])
@pytest.mark.parametrize("quality", ["720", "1080"])
def test_a_capped_quality_never_re_encodes(quality, formats):
    """Both capped settings exist to be fast and lossless. If either ever
    picks a container that isn't .mp4, someone is waiting on ffmpeg for a
    stream that was already playable."""
    if quality == "720" and formats is HAS_4K:
        pytest.skip("this table has no <=720p rung; real uploads always do")
    video, container = _select(quality, formats)
    assert "avc1" in video["vcodec"]
    assert not _reencodes(container)


def test_720_picks_exactly_what_cinema_itself_would():
    """The default has to stay indistinguishable from the mod's own download —
    Cinema hardcodes this same format string in VideoQuality.cs."""
    video, container = _select("720", TOPS_OUT_AT_1080)
    assert video["format_id"] == "136"
    assert not _reencodes(container)


def test_1080_stops_at_1080_even_when_4k_exists():
    video, _ = _select("1080", HAS_4K)
    assert video["height"] == 1080


# ── Audio pairing ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("quality", app_config.VIDEO_QUALITIES)
def test_an_h264_pick_never_gets_dragged_into_mkv_by_its_audio(quality):
    """Opus outranks m4a on bitrate, and an H.264 video paired with Opus
    merges to .mkv — a transcode caused entirely by the audio track."""
    video, container = _select(quality, TOPS_OUT_AT_1080)
    assert "avc1" in video["vcodec"]
    assert not _reencodes(container)


# ── Recode settings reaching ffmpeg ──────────────────────────────────────────


def _convertor_command(quality, monkeypatch, tmp_path):
    """The ffmpeg command line yt-dlp's convertor step would actually run.

    ``--postprocessor-args`` is easy to get subtly wrong — an unrecognised
    postprocessor name is not an error, the arguments are just silently
    dropped and the transcode quietly falls back to ffmpeg's defaults.
    Asserting on the string we pass in cannot catch that.

    So this goes all the way down to the process boundary. yt-dlp splices
    postprocessor args in inside ``real_run_ffmpeg`` — below ``run_ffmpeg``,
    which is why intercepting higher up would miss them entirely — and the
    assembled argv is what finally proves they landed.
    """
    from yt_dlp.postprocessor.ffmpeg import FFmpegVideoConvertorPP
    from yt_dlp.utils import Popen

    args = app_config.video_download_args(quality)
    opts = {"quiet": True, "no_warnings": True}
    if "--postprocessor-args" in args:
        name, _, values = args[args.index("--postprocessor-args") + 1].partition(":")
        opts["postprocessor_args"] = {name.lower(): values.split()}

    source = tmp_path / "merged.mkv"
    source.write_bytes(b"not really a video")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = [str(c) for c in cmd]
        return "", "", 0

    # Build the postprocessor *before* faking Popen: yt-dlp probes ffmpeg's
    # version through the very same call, so patching first makes it conclude
    # ffmpeg isn't installed. Real detection, faked execution.
    pp = FFmpegVideoConvertorPP(yt_dlp.YoutubeDL(opts), preferedformat="mp4")
    if not pp.available:  # resolves lazily, and probes ffmpeg to do it
        pytest.skip("ffmpeg not found by yt-dlp")
    monkeypatch.setattr(Popen, "run", staticmethod(fake_run))
    pp.run({"filepath": str(source), "ext": "mkv", "id": "t", "title": "t"})
    return captured.get("cmd", [])


_needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None,
                                   reason="ffmpeg not installed")


@_needs_ffmpeg
def test_max_really_hands_the_crf_to_ffmpeg(monkeypatch, tmp_path):
    monkeypatch.setattr(app_config, "config_path", lambda: tmp_path / "c.json")
    assert app_config.set_transcode_crf(14)
    cmd = _convertor_command("max", monkeypatch, tmp_path)
    assert "-crf" in cmd, cmd
    assert cmd[cmd.index("-crf") + 1] == "14", cmd


@_needs_ffmpeg
def test_max_really_tells_ffmpeg_to_copy_the_audio(monkeypatch, tmp_path):
    monkeypatch.setattr(app_config, "config_path", lambda: tmp_path / "c.json")
    cmd = _convertor_command("max", monkeypatch, tmp_path)
    assert "-c:a" in cmd, cmd
    assert cmd[cmd.index("-c:a") + 1] == "copy", cmd


@_needs_ffmpeg
def test_the_crf_lands_after_the_output_not_the_input(monkeypatch, tmp_path):
    """An encoder setting before -i applies to decoding and is ignored. It
    has to sit on the output side to mean anything."""
    monkeypatch.setattr(app_config, "config_path", lambda: tmp_path / "c.json")
    cmd = _convertor_command("max", monkeypatch, tmp_path)
    assert cmd.index("-crf") > cmd.index("-i"), cmd


@_needs_ffmpeg
@pytest.mark.parametrize("quality", ["720", "1080"])
def test_a_capped_quality_adds_no_ffmpeg_arguments(quality, monkeypatch, tmp_path):
    monkeypatch.setattr(app_config, "config_path", lambda: tmp_path / "c.json")
    cmd = _convertor_command(quality, monkeypatch, tmp_path)
    assert "-crf" not in cmd
    assert "-c:a" not in cmd
