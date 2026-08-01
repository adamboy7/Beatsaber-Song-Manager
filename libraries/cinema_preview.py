"""Cinema offset preview engines — one clock (default) and two (fallback).

The offset editor previews a Cinema offset by playing the song and the video
together and letting the user judge the result, optionally in split stereo
(video in the left ear, song in the right). How those two streams are driven
is the whole question this module exists to answer.

The problem with two clocks
---------------------------
``TwoInstancePreview`` runs two libmpv instances — one for the song, one for
the video — and holds them together with a 10 Hz watchdog. Two instances mean
two independent playback clocks, so the best that watchdog can promise is a
*bound* on the error: ``DRIFT_TOLERANCE_SPLIT_S`` is 20 ms. Twenty milliseconds
between two correlated signals in opposite ears is audible as a smear rather
than a doubling, which is exactly the artefact this module exists to remove.
Worse, ``drift_speed_trim`` corrects by easing playback speed ±3%, so while a
correction is in flight the two ears are running at genuinely different rates
and the smear is time-varying.

One clock
---------
``SinglePlayerPreview`` puts both streams in one libmpv instance, which makes
*drift* structurally impossible: one clock cannot diverge from itself.

That is narrower than it first sounds, and the distinction cost a round of
debugging. A single clock rules out error that accumulates; it says nothing
about a constant offset introduced when the two streams were first positioned.
A seek that rounds to a keyframe is exactly that, and by ear it is
indistinguishable from a wrong offset — hence ``hr_seek``, and hence
``_measure_seek_error`` reporting where the seek actually landed instead of
trusting that it landed anywhere in particular.

The video becomes the master file and the audio is attached as one external
track, so:

    master_time == video_time,  and  video_time = song_time + offset

is the same relation the rest of the app uses (``VisualizerWindow._video_pos``,
``cinema_offset_window.video_time_to_x``), just read as a timeline rather than
as a seek target.

Getting the song onto that timeline means shifting it by ``offset``, and the
shift is *baked into the audio data*: silence prepended, or the head trimmed.
Baking is a memory slice, not an ffmpeg pass — ``PcmSource`` decodes each
source to raw PCM once and every shift after that is a prefix of zeros plus a
copy — so it is cheap enough to redo on each offset change, on a worker thread.

One audio track, mixed here
---------------------------
Split stereo used to be an mpv ``lavfi-complex`` graph mixing two tracks into
opposite ears. It was wrong on real files: a capture measured the video ear a
constant 848 ms behind the song ear while mpv's own ``avsync`` read zero,
because from mpv's point of view nothing *was* wrong — the graph was being fed
by two demuxers that had not resumed from the same instant after a seek.

So the mix moved in here (``mix_preview``). Both sources are decoded up front,
placed on the master timeline, panned and summed into a single stereo buffer,
and mpv is handed one ordinary audio track to play. Sample offsets within one
array cannot disagree the way two demuxers can, and the ear alignment — the
thing the whole feature exists to let people judge — no longer depends on mpv
at all.

Why not ``audio-delay``
-----------------------
mpv can shift audio against video directly, which would make a nudge a single
property write. Two things rule it out as the mechanism:

* A Cinema offset can be tens of seconds (the mod's bundled config for "Crab
  Rave" ships +34900 ms), and the filter chain would have to buffer that far
  ahead of the playback position.
* More importantly, it is not reliably honoured. A build that accepts the
  property and ignores it yields a preview that is consistently off while every
  piece of arithmetic above it is correct — which is precisely the bug that
  moved this module to exact baking. ``_verify_audio_delay`` writes and reads
  the property back at startup so the engine at least *knows*.

``audio-delay`` is still written on every offset change, because it is instant
where a bake is tens of milliseconds of I/O. But it is only ever an
approximation held until the bake lands; nothing rests on it. ``BAKE_BUCKET_MS``
remains available for callers that want to trade that guarantee back.

Property access
---------------
Read and write mpv properties through ``prop_get``/``prop_set``, never
``player[name]`` — that is python-mpv's *option* namespace, and the properties
this module depends on most (``track-list``, ``time-pos``) do not exist in it.

Everything here is blocking and safe to call off the Tk thread except where a
docstring says otherwise; nothing here imports tkinter.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path

from libraries.audio_utils import find_ffmpeg
from libraries.mpv_backend import load_mpv

# ── PCM format ───────────────────────────────────────────────────────────────
# Fixed rather than inherited from the source file so the shift arithmetic is
# exact: 48000 Hz × 2 channels × 2 bytes is 192 bytes per millisecond with no
# remainder, so any whole-millisecond shift lands on a sample boundary and
# needs no rounding anywhere.
SAMPLE_RATE = 48_000
CHANNELS = 2
SAMPLE_WIDTH = 2
BYTES_PER_MS = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH // 1000  # 192

# A canonical PCM WAV header. Used only to recognise a file with no samples in
# it, which is what a shift that trimmed the entire song produces.
_WAV_HEADER_BYTES = 44

# How far the opening seek may land from its target before it's worth saying so.
# One video frame at 24 fps is 42 ms; anything past this is not rounding.
_SEEK_TOLERANCE_S = 0.05

# Optional quantisation of the baked shift, leaving the remainder to mpv's
# audio-delay so a nudge is a property write rather than a rebake. **Not used
# by default** — the engine bakes exactly (``bucket_ms=0``).
#
# It was the default once. The trouble is that it makes correctness depend on
# mpv honouring audio-delay, and a build that accepts the property and ignores
# it produces a preview that is consistently off by up to half a bucket while
# every piece of arithmetic above it is right. Baking exactly puts the offset
# somewhere nothing downstream can disregard. Pass ``bucket_ms=BAKE_BUCKET_MS``
# to trade that guarantee back for cheaper nudges.
BAKE_BUCKET_MS = 500

# Cinema pans the video hard left and the map to 0.9 rather than 1.0. Its
# PlaybackController.cs explains why: "only pan mostly right, because for some
# reason the video player audio doesn't pan hard left either. Also, it sounds
# a bit more comfortable." ffmpeg's pan filter separates exactly, so only the
# second half of that reasoning applies to us — the comfort bleed is
# reproduced deliberately rather than inherited from a Unity quirk.
EAR_BLEED = 0.1

# ── Two-instance drift correction ────────────────────────────────────────────
# Only the two-instance engine uses these; the single-instance one has no
# drift to correct. They live here rather than in cinema_offset_window because
# that module now imports its engines from this one, and the window has no
# business owning constants it never reads.

# Resync the preview video when it drifts further than this from where the
# current offset says it should be. Split-stereo listening is far less
# forgiving than watching, so it gets the tighter figure.
DRIFT_TOLERANCE_S = 0.12
DRIFT_TOLERANCE_SPLIT_S = 0.02

# Above this much error, a seek is the only way back; below it, drift is
# corrected by easing the video's playback speed, which is inaudible where a
# seek would be an obvious click in one ear.
DRIFT_SEEK_THRESHOLD_S = 0.30
DRIFT_EASE_SECONDS = 2.0      # pull the error out over roughly this long
DRIFT_MAX_SPEED_TRIM = 0.03   # ±3%; mpv pitch-corrects, so this is unheard


def drift_speed_trim(error_s: float) -> float:
    """Playback speed that eases ``error_s`` of drift out over a couple of seconds.

    ``error_s`` is *actual − expected*: positive means the video is running
    ahead, so it should play slightly slower. Clamped hard — this is a trim,
    not a scrub.
    """
    trim = max(-DRIFT_MAX_SPEED_TRIM,
               min(DRIFT_MAX_SPEED_TRIM, -error_s / DRIFT_EASE_SECONDS))
    return 1.0 + trim


def pan_filter(left_gain: float, right_gain: float) -> str:
    """An mpv ``af`` string downmixing to stereo at the given per-ear gains.

    The two-instance engine's panning: each player gets its own ``af``, since
    the two are separate processes and there is no shared graph to put them in.

    ``aformat`` first because ``pan`` addresses input channels positionally:
    a mono song (``c1`` undefined) would fail the graph outright, and a 5.1
    video track would drop everything but front-left/right. The halving keeps
    a summed downmix from clipping.

    Wrapped in ``lavfi=[...]`` so mpv hands the whole graph to libavfilter
    without splitting on the ``,`` and ``|`` inside it.

    Near-duplicates ``pan_chain`` below, deliberately: that one adds a sample
    rate clamp that ``amix`` needs and this one must not have, since forcing a
    resample on a path that doesn't need one would change what the shipping
    engine sounds like. The ``pan=`` expressions are held identical by a test.
    """
    l, r = left_gain / 2.0, right_gain / 2.0
    return (
        "lavfi=[aformat=channel_layouts=stereo,"
        f"pan=stereo|c0={l:.4f}*c0+{l:.4f}*c1|c1={r:.4f}*c0+{r:.4f}*c1]"
    )


class PreviewError(RuntimeError):
    """The preview could not be prepared or started."""


# ── mpv property access ──────────────────────────────────────────────────────
#
# python-mpv's ``player[name]`` is *option* access — it prefixes the lookup with
# ``options/``. Properties are a different namespace, and the ones this module
# needs most (``track-list``, ``time-pos``) exist only as properties, so item
# access fails them with "mpv property does not exist". The rest of the app
# never hit this because it only ever touches names that happen to be both
# (``volume``, ``speed``, ``mute``, ``af``, ``pause``) and reaches them by
# attribute anyway.
#
# ``_get_property``/``_set_property`` take the real dashed name and work for
# every property, including ones python-mpv hasn't bound as an attribute.
# Attribute access is the documented API though, so it is tried first and these
# are the fallback — belt and braces across binding versions.


def _attr_name(name: str) -> str:
    return name.replace("-", "_")


def prop_get(player, name: str):
    """Read an mpv property, or None if it's unavailable.

    ``name`` may use dashes or underscores; both reach the same property.
    """
    if player is None:
        return None
    dashed = name.replace("_", "-")
    if isinstance(getattr(type(player), _attr_name(dashed), None), property):
        try:
            return getattr(player, _attr_name(dashed))
        except Exception:
            return None
    try:
        return player._get_property(dashed)
    except Exception:
        return None


def prop_set(player, name: str, value) -> bool:
    """Write an mpv property. Returns whether it took.

    Never falls back to a plain ``setattr``: on a name python-mpv hasn't bound,
    that would quietly create an instance attribute and report success while
    mpv carried on unchanged.
    """
    if player is None:
        return False
    dashed = name.replace("_", "-")
    if isinstance(getattr(type(player), _attr_name(dashed), None), property):
        try:
            setattr(player, _attr_name(dashed), value)
            return True
        except Exception:
            return False
    try:
        player._set_property(dashed, value)
        return True
    except Exception:
        return False


# ── Timeline math ────────────────────────────────────────────────────────────
# Free functions, no mpv, no I/O: this is the part worth testing, and the test
# suite never opens a player.


def master_from_song(song_time_s: float, offset_ms: int) -> float:
    """Master-timeline position showing ``song_time_s``.

    The master timeline *is* the video's, so this is just Cinema's own
    ``video_time = song_time + offset`` — the same expression as
    ``VisualizerWindow._video_pos``.
    """
    return song_time_s + offset_ms / 1000.0


def song_from_master(master_time_s: float, offset_ms: int) -> float:
    """Song position audible at ``master_time_s`` — the inverse of the above."""
    return master_time_s - offset_ms / 1000.0


def bake_shift_ms(offset_ms: int, bucket_ms: int = 0) -> int:
    """How much of ``offset_ms`` to bake into the audio, quantised to a bucket.

    ``bucket_ms`` of 0 — the default — bakes the offset exactly, leaving no
    residual for ``audio-delay`` to carry.

    Positive means silence is prepended to the song; negative means its head is
    trimmed. Both signs are handled the same way on purpose — a negative offset
    means the video starts *later* than the song, so the trimmed region is
    exactly the range where ``video_time`` is still negative and there is no
    video to compare against. Nothing previewable is lost, which is the same
    reasoning ``_check_video_drift`` already applies when it bails out with
    "video legitimately isn't showing at this point".
    """
    if bucket_ms <= 0:
        return int(offset_ms)
    return int(round(offset_ms / bucket_ms)) * bucket_ms


def residual_delay_ms(offset_ms: int, baked_shift_ms: int) -> int:
    """The part of the offset mpv's ``audio-delay`` still has to supply.

    Bounded by half a bucket immediately after a bake, and by
    quantisation is in use; always zero with the default exact bake.
    """
    return int(offset_ms) - int(baked_shift_ms)


def pan_chain(left_gain: float, right_gain: float) -> str:
    """A libavfilter chain downmixing one track to stereo at the given gains.

    The body is deliberately identical to what ``cinema_offset_window.pan_filter``
    produces — the ear assignment is not arbitrary, since Cinema's README tells
    users "if the sound from the left ear is behind, use the '+' buttons" and
    reversing it would invert advice people already have. The differences from
    that function are both structural: no ``lavfi=[...]`` wrapper (this goes
    into a filter graph directly rather than through mpv's ``af`` parser), and
    an explicit sample rate so ``amix`` never has to reconcile two tracks that
    decoded at different rates.

    ``aformat`` comes first because ``pan`` addresses channels positionally: a
    mono song (``c1`` undefined) would fail the graph outright and a 5.1 video
    track would lose everything but front-left/right. The halving keeps the
    summed downmix from clipping.
    """
    l, r = left_gain / 2.0, right_gain / 2.0
    return (
        f"aformat=sample_rates={SAMPLE_RATE}:channel_layouts=stereo,"
        f"pan=stereo|c0={l:.4f}*c0+{l:.4f}*c1|c1={r:.4f}*c0+{r:.4f}*c1"
    )


def _audioop():
    """The stdlib audio primitives, or a PreviewError explaining their absence.

    ``audioop`` was removed from the standard library in Python 3.13; the
    ``audioop-lts`` backport is a declared dependency and restores it under the
    same name.
    """
    try:
        import audioop
        return audioop
    except ImportError as exc:
        raise PreviewError(
            "the 'audioop-lts' package is missing, so split stereo can't be "
            f"mixed ({exc})"
        )


def pan_ears(pcm: bytes, left_gain: float, right_gain: float) -> bytes:
    """Downmix ``pcm`` to mono and place it at the given per-ear gains.

    The in-process equivalent of ``pan_chain``: ``tomono`` averages the two
    input channels — which is what ``pan``'s ``l*c0 + l*c1`` amounts to once
    the halving is folded in — and ``tostereo`` distributes the result. A test
    holds this to sample parity with what ffmpeg's ``pan`` produces, because
    the ear assignment is the part users have been given written instructions
    about.
    """
    ap = _audioop()
    mono = ap.tomono(pcm, SAMPLE_WIDTH, 0.5, 0.5)
    return ap.tostereo(mono, SAMPLE_WIDTH, left_gain, right_gain)


def mix_preview(song_pcm: bytes, video_pcm: bytes | None, shift_ms: int, *,
                split: bool, ear_bleed: float = EAR_BLEED) -> bytes:
    """Build exactly what should be heard, as one stereo PCM buffer.

    Both sources are placed on the *master* (video) timeline: the video's audio
    sits where it already is, and the song is shifted by ``shift_ms``. The two
    are then panned to opposite ears and summed here, in a buffer this module
    owns.

    Doing the mix ourselves rather than in an mpv ``lavfi-complex`` graph is the
    whole point. That graph fed two independent demuxers into ``amix``, and
    after a seek they did not resume from the same instant — a capture measured
    the video ear a constant 848 ms late while mpv's own ``avsync`` read zero,
    because from mpv's side nothing *was* wrong. Sample offsets in one array
    cannot disagree like that; there is no second clock, no second demuxer, and
    nothing left for mpv to align.

    With ``split`` off this is simply the shifted song, and the video's own
    audio is left out entirely (the caller mutes it).
    """
    silence, body = wav_bytes_for_shift(song_pcm, shift_ms)
    song = silence + body
    if not split or not video_pcm:
        return song

    # Pad both to a common length: a video shorter than the song must not
    # truncate it, and vice versa.
    n = max(len(song), len(video_pcm))
    song = song.ljust(n, b"\0")
    video = bytes(video_pcm).ljust(n, b"\0")
    return _audioop().add(
        pan_ears(video, 1.0, ear_bleed),   # video hard-ish left
        pan_ears(song, ear_bleed, 1.0),    # song right
        SAMPLE_WIDTH,
    )


# ── Baking ───────────────────────────────────────────────────────────────────


def wav_bytes_for_shift(pcm: bytes, shift_ms: int) -> tuple[bytes, bytes]:
    """Return ``(silence_prefix, pcm_slice)`` for a song shifted by ``shift_ms``.

    Split rather than concatenated so the caller can stream both into a file
    without ever holding two copies of a thirty-megabyte song in memory.

    A shift past the end of the song yields no audio at all, which is correct:
    the offset has moved the entire song outside the video's timeline.
    """
    shift_ms = int(shift_ms)
    if shift_ms >= 0:
        return bytes(shift_ms * BYTES_PER_MS), pcm
    drop = min(len(pcm), -shift_ms * BYTES_PER_MS)
    return b"", pcm[drop:]


class PcmSource:
    """A song decoded once to raw PCM, so every reshift is a memory slice.

    Decoding is the expensive part (an ffmpeg pass over the whole file);
    shifting is a prefix of zeros plus a copy. Keeping the decode separate is
    what makes a rebake cheap enough to do on a bucket crossing rather than
    something to design around.
    """

    def __init__(self, pcm: bytes, source: Path):
        self.pcm = pcm
        self.source = Path(source)

    @property
    def duration_s(self) -> float:
        return len(self.pcm) / (BYTES_PER_MS * 1000.0)

    @classmethod
    def decode(cls, path: Path, *, ffmpeg: str | None = None,
               timeout: int = 180) -> "PcmSource":
        """Decode ``path`` to interleaved s16le at ``SAMPLE_RATE``.

        Blocking, and safe to call from a worker thread — nothing here touches
        shared state. Raises ``PreviewError`` on any failure.
        """
        path = Path(path)
        if not path.exists():
            raise PreviewError(f"{path.name} does not exist")
        exe = ffmpeg or find_ffmpeg()
        if not exe:
            raise PreviewError("ffmpeg is not available")

        cmd = [
            exe, "-hide_banner", "-nostdin", "-v", "error",
            "-i", str(path),
            "-vn",  # a cover-art stream would otherwise be decoded for nothing
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
            "-",
        ]
        try:
            result = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise PreviewError(f"decoding {path.name} timed out after {timeout}s")
        except OSError as exc:
            raise PreviewError(f"could not run ffmpeg: {exc}")

        if result.returncode != 0 or not result.stdout:
            err = (result.stderr or b"").decode("utf-8", errors="replace").strip()
            raise PreviewError(err or f"ffmpeg exited {result.returncode}")
        return cls(result.stdout, path)

    def write_shifted_wav(self, out: Path, shift_ms: int, *,
                          video_pcm: bytes | None = None,
                          split: bool = False) -> Path:
        """Write what should be heard — shifted, panned and mixed — to ``out``.

        Blocking; call from a worker thread. Returns ``out``.
        """
        data = mix_preview(self.pcm, video_pcm, shift_ms, split=split)
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out), "wb") as wav:
            wav.setnchannels(CHANNELS)
            wav.setsampwidth(SAMPLE_WIDTH)
            wav.setframerate(SAMPLE_RATE)
            if data:
                wav.writeframes(data)
        return out


# ── Track discovery ──────────────────────────────────────────────────────────


class AudioTracks:
    """Which mpv track id is the video's own audio, and which is the song.

    ``external`` is the discriminator rather than position: mpv numbers audio
    tracks in load order, so a video with no audio track of its own leaves the
    added song sitting at ``aid1``. Assuming ``aid2`` in that case would send
    the song into both ears and present as a sync fault rather than a
    misrouting, which is a bad way to spend an evening.
    """

    def __init__(self, internal: int | None, external: int | None):
        self.internal = internal
        self.external = external

    @property
    def can_split(self) -> bool:
        """Split stereo needs two real tracks to put in two ears."""
        return self.internal is not None and self.external is not None

    @classmethod
    def from_track_list(cls, tracks, *, external_id: int | None = None) -> "AudioTracks":
        """Classify the audio tracks, preferring a known-added id.

        ``external_id`` is what the caller observed appear after ``audio-add``,
        and it wins over anything the track list claims. Diffing ids is the
        only version-independent way to do this: whether a track reports
        ``external`` at all — and whether the flag is a bool, an int, or absent
        in favour of ``external-filename`` — varies between libmpv builds, and
        guessing wrong presents as "the song track did not attach" on a file
        that plays perfectly well.
        """
        internal = external = None
        for track in tracks or []:
            try:
                if track.get("type") != "audio":
                    continue
                tid = int(track.get("id"))
            except (AttributeError, TypeError, ValueError):
                continue
            if tid == external_id or cls._looks_external(track):
                if external is None:
                    external = tid
            elif internal is None:
                internal = tid
        return cls(internal, external)

    @staticmethod
    def _looks_external(track) -> bool:
        """Whether a track entry describes a file mpv was told to add.

        ``external-filename`` is checked as well as ``external`` because some
        builds populate only the former.
        """
        try:
            return bool(track.get("external")) or bool(track.get("external-filename"))
        except AttributeError:
            return False


def audio_track_ids(tracks) -> set[int]:
    """Every audio track id in an mpv ``track-list``.

    Snapshotting this before and after ``audio-add`` is how the added track is
    identified without trusting any particular build's flags.
    """
    ids = set()
    for track in tracks or []:
        try:
            if track.get("type") == "audio":
                ids.add(int(track.get("id")))
        except (AttributeError, TypeError, ValueError):
            continue
    return ids


def describe_tracks(tracks) -> str:
    """A short human-readable summary, for when attaching went wrong.

    Ends up in the editor's status line, so it has to say something a user can
    act on — or at least repeat back — rather than dumping an mpv node.
    """
    parts = []
    for track in tracks or []:
        try:
            if track.get("type") != "audio":
                continue
            parts.append(f"#{track.get('id')}"
                         f"{' external' if AudioTracks._looks_external(track) else ''}"
                         f" {track.get('codec') or '?'}")
        except AttributeError:
            continue
    return ", ".join(parts) or "no audio tracks at all"


# ── Engines ──────────────────────────────────────────────────────────────────
#
# Both engines share the surface below so the A/B harness — and, if this wins,
# CinemaOffsetWindow — can swap one for the other without knowing which is
# which:
#
#     start(at_song_s) / stop() / is_running
#     set_offset_ms(ms, settling=False)
#     set_split(bool)
#     song_position_s() -> float | None
#     poll()                       (called at ~10 Hz from the Tk tick)
#     measured_drift_s() -> float | None
#     status -> str
#
# ``settling=True`` means "an interaction is still in progress" (a drag), and
# invites the engine to defer anything expensive until it goes False.


class SinglePlayerPreview:
    """One libmpv instance: video as master, song as an external audio track.

    Drift correction is absent by design — there is one clock, so there is
    nothing to correct. ``measured_drift_s`` returns 0.0 rather than None to
    make that a positive claim in the harness readout rather than a gap.
    """

    def __init__(self, video_path: Path, pcm: PcmSource, *,
                 video_pcm: bytes | None = None,
                 wid: int | None = None, volume: int | None = None,
                 bucket_ms: int = 0):
        self._video_path = Path(video_path)
        self._pcm = pcm
        # The video's own audio, decoded up front so split stereo can be mixed
        # here rather than in an mpv filter graph. None when the video has no
        # audio track, which simply makes split stereo unavailable.
        self._video_pcm = video_pcm
        self._baked_split = False
        self._wid = wid
        self._volume = volume
        self._bucket_ms = bucket_ms

        self._player = None
        self._tracks = AudioTracks(None, None)
        self._offset_ms = 0
        self._baked_shift_ms = 0
        self._split = False
        self._status = ""
        self._ended = False

        # Whether audio-delay was observed to actually take on this build; see
        # _verify_audio_delay. False means the exact bake is doing all the work.
        self._delay_is_live = True
        # How far the opening seek missed by, measured rather than assumed.
        self._seek_error_s = 0.0

        self._tmpdir: Path | None = None
        # Alternating names so a rebake never writes over the file the running
        # player still has open — which on Windows would simply fail.
        self._bake_parity = 0
        self._bake_lock = threading.Lock()
        self._baking = False
        # Bumped by stop(), so a bake still in flight abandons its swap rather
        # than talking to a player that has been torn down underneath it.
        self._bake_gen = 0

    # ── Lifecycle ────────────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._player is not None

    @property
    def status(self) -> str:
        return self._status

    def start(self, at_song_s: float, offset_ms: int, *, split: bool = False) -> None:
        """Load both streams and begin playing from ``at_song_s``.

        Blocking: it bakes a WAV and waits for mpv to demux the video before it
        can read the track list. Fine on a worker thread, and the reason the
        harness starts it off the Tk thread.

        Raises ``PreviewError`` if libmpv or the video is unavailable.
        """
        mpv_mod = load_mpv()
        if mpv_mod is None:
            raise PreviewError("libmpv is not available")
        if not self._video_path.exists():
            raise PreviewError(f"{self._video_path.name} does not exist")

        self.stop()
        self._offset_ms = int(offset_ms)
        self._split = bool(split)
        self._ended = False
        self._tmpdir = Path(tempfile.mkdtemp(prefix="bssm-cinema-preview-"))

        self._baked_shift_ms = bake_shift_ms(self._offset_ms, self._bucket_ms)
        self._baked_split = self._split and self._can_split
        wav = self._write_bake(self._baked_shift_ms, self._baked_split)
        if wav.stat().st_size <= _WAV_HEADER_BYTES:
            # A shift that trimmed the whole song away. mpv would reject the
            # empty file and the failure would surface as a missing track, so
            # name the real cause instead.
            self._cleanup_tmp()
            raise PreviewError(
                f"an offset of {self._offset_ms:+d} ms moves the whole song "
                "outside the video's timeline"
            )

        kwargs = dict(
            idle="yes", keep_open="yes", osd_level=0,
            # Exact seeking, and it is load-bearing rather than a nicety. By
            # default mpv may satisfy an absolute seek by landing on the nearest
            # preceding keyframe, and Cinema videos are web rips with one- to
            # two-second GOPs. The song is a WAV and seeks sample-exact, so a
            # keyframe-rounded video seek puts the picture up to a GOP behind
            # the audio — constant, and indistinguishable from a wrong offset.
            # The two-instance engine hides this because its watchdog re-seeks
            # the video continuously; with one clock there is nothing to
            # correct it afterwards, so the seek itself has to be right.
            hr_seek="yes",
            input_default_bindings=False, input_vo_keyboard=False,
            input_cursor=False, cursor_autohide="no",
        )
        if self._wid is not None:
            kwargs["wid"] = str(self._wid)
        else:
            kwargs["vid"] = "no"
        try:
            player = mpv_mod.MPV(**kwargs)
        except Exception as exc:
            self._cleanup_tmp()
            raise PreviewError(f"could not create the player: {exc}")

        if self._volume is not None:
            try:
                player.volume = self._volume
            except Exception:
                pass

        try:
            player.observe_property("eof-reached", self._on_eof)
        except Exception:
            pass

        master = max(0.0, master_from_song(at_song_s, self._offset_ms))
        try:
            player.loadfile(str(self._video_path), start=f"{master:.3f}")
            # The track list is empty until the demuxer has parsed the file, so
            # there is no way to attach the song or read ids before this lands.
            player.wait_for_property("duration", timeout=10)
            before = audio_track_ids(prop_get(player, "track-list"))
            player.command("audio-add", str(wav), "select")
            added, tracks = self._await_added_track(player, before)
        except PreviewError:
            try:
                player.terminate()
            except Exception:
                pass
            self._cleanup_tmp()
            raise
        except Exception as exc:
            try:
                player.terminate()
            except Exception:
                pass
            self._cleanup_tmp()
            raise PreviewError(f"could not load the preview: {exc}")

        self._player = player
        self._tracks = AudioTracks.from_track_list(tracks, external_id=added)
        # Re-seek now that both streams are attached. The track was added
        # *after* loadfile's own seek, and a demuxer that joined late is not
        # necessarily sitting where the video is — a seek with both present is
        # what forces mpv to line them up, and skipping it leaves a constant
        # error that no amount of correct arithmetic upstream will fix.
        self._seek_master(master)
        self._seek_error_s = self._measure_seek_error(master)
        self._delay_is_live = self._verify_audio_delay()
        self._apply_delay()
        self._apply_routing()
        if abs(self._seek_error_s) > _SEEK_TOLERANCE_S:
            # Reported rather than silently absorbed: this is the difference
            # between "the offset is wrong" and "the seek missed", and from the
            # listener's chair those sound identical.
            self._status = (
                f"The video seek landed {self._seek_error_s * 1000:+.0f} ms from "
                "where it was asked to — the picture may sit behind the sound."
            )

    def _measure_seek_error(self, wanted_master_s: float,
                            timeout_s: float = 2.0) -> float:
        """Where playback actually settled, minus where it was sent.

        Negative means the video landed *early* — the keyframe-rounding
        signature. Worth measuring rather than assuming: every other candidate
        for a constant offset lives in arithmetic this module controls, and
        this one does not.
        """
        deadline = time.monotonic() + timeout_s
        pos = None
        while time.monotonic() < deadline:
            pos = self._get("time-pos")
            if pos is not None:
                break
            time.sleep(0.05)
        if pos is None:
            return 0.0
        return float(pos) - wanted_master_s

    def _verify_audio_delay(self) -> bool:
        """Whether ``audio-delay`` actually takes on this build.

        Written and read back, because the alternative to knowing is a preview
        that is silently wrong by up to the residual. When it doesn't hold, the
        engine stops relying on it and bakes the offset exactly instead — which
        is slower but cannot be ignored by anything downstream.
        """
        probe = 0.25
        if not self._set("audio-delay", probe):
            return False
        got = self._get("audio-delay")
        self._set("audio-delay", 0.0)
        try:
            return abs(float(got) - probe) < 0.005
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _await_added_track(player, before: set[int], timeout_s: float = 5.0):
        """Wait for ``audio-add``'s track to show up. Returns ``(id, track_list)``.

        ``audio-add`` returns before the added file has necessarily been
        demuxed and published to ``track-list``, so reading the list straight
        afterwards is a race — one that resolves as "the song track did not
        attach" on a video that is otherwise perfectly fine.

        Polling for a *new id* rather than for an ``external`` flag is
        deliberate; see ``AudioTracks.from_track_list``.

        Raises ``PreviewError`` if nothing appears, which is what lets
        ``start_preview`` fall back to the two-instance engine rather than
        leaving a dead preview on screen.
        """
        deadline = time.monotonic() + timeout_s
        tracks = []
        while True:
            tracks = prop_get(player, "track-list") or []
            new = audio_track_ids(tracks) - before
            if new:
                return min(new), tracks
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        raise PreviewError(
            "libmpv did not attach the song as an extra audio track "
            f"(it reports: {describe_tracks(tracks)})"
        )

    def stop(self) -> None:
        with self._bake_lock:
            # Orphans any bake in flight: it will see the new generation and
            # abandon its swap rather than command a terminated player.
            self._bake_gen += 1
        player, self._player = self._player, None
        if player is not None:
            try:
                player.terminate()
            except Exception:
                pass
        self._cleanup_tmp()
        self._status = ""

    # ── Offset ───────────────────────────────────────────────────────────────

    def set_offset_ms(self, offset_ms: int, *, settling: bool = False) -> None:
        """Apply a new offset, rebaking only if the bucket has been left.

        With the default exact bake, the offset ends up baked into the audio
        data, where nothing downstream can ignore it. ``audio-delay`` is still
        written immediately as a first approximation — it's instant, where a
        bake is tens of milliseconds of I/O — but it is no longer what the
        result *rests* on, and on a build where it does nothing the preview
        simply snaps correct when the bake lands instead of staying wrong.

        The bake runs on a worker thread: it writes tens of megabytes, and
        doing that inline would stutter the UI on every nudge. Requests
        coalesce, so holding an arrow key down bakes once at the end rather
        than once per repeat.

        ``settling`` (mid-drag) skips the bake entirely and rides on the delay
        until the drag ends.
        """
        offset_ms = int(offset_ms)
        changed = offset_ms != self._offset_ms
        self._offset_ms = offset_ms
        if self._player is None:
            return
        if changed:
            self._apply_delay()
        # Not conditional on ``changed``: a drag ends on the value its last
        # motion event already set, so returning early when nothing moved would
        # skip the very bake that ``settling`` deferred — the offset would then
        # only take effect on the next nudge, or on a restart. ``_request_bake``
        # is idempotent, so an unnecessary call costs nothing.
        if not settling:
            self._request_bake()

    @property
    def baked_shift_ms(self) -> int:
        """The shift currently baked into the attached audio."""
        return self._baked_shift_ms

    @property
    def residual_delay_ms(self) -> int:
        """Offset not yet baked in, and so resting on ``audio-delay``.

        Zero in the steady state with exact baking. Non-zero only between a
        nudge and its bake landing — or permanently, if a bucket was chosen.
        """
        return residual_delay_ms(self._offset_ms, self._baked_shift_ms)

    @property
    def delay_is_live(self) -> bool:
        """Whether ``audio-delay`` was observed to work on this build."""
        return self._delay_is_live

    def _apply_delay(self) -> None:
        self._set("audio-delay", self.residual_delay_ms / 1000.0)

    def _target_shift_ms(self) -> int:
        return bake_shift_ms(self._offset_ms, self._bucket_ms)

    def _target_key(self) -> tuple[int, bool]:
        """What the attached audio should contain: shift *and* ear routing.

        Split is part of the bake now, not an mpv filter, so toggling the
        checkbox needs a rebuild just as a nudge does.
        """
        return self._target_shift_ms(), self._split and self._can_split

    def _baked_key(self) -> tuple[int, bool]:
        return self._baked_shift_ms, self._baked_split

    @property
    def _can_split(self) -> bool:
        return bool(self._video_pcm)

    def _request_bake(self) -> None:
        """Queue a bake for the current offset, coalescing with any in flight."""
        with self._bake_lock:
            if self._target_key() == self._baked_key():
                return
            if self._baking:
                return  # the running worker re-reads the target when it loops
            self._baking = True
            gen = self._bake_gen
        threading.Thread(target=self._bake_worker, args=(gen,), daemon=True).start()

    def _bake_worker(self, gen: int) -> None:
        """Bake and swap until the attached audio matches the current offset.

        Loops rather than returning after one pass so a nudge that arrived
        mid-bake is picked up without needing its own thread.
        """
        while True:
            with self._bake_lock:
                target = self._target_key()
                if gen != self._bake_gen or self._player is None \
                        or target == self._baked_key():
                    self._baking = False
                    return
            try:
                wav = self._write_bake(*target)
            except Exception as exc:
                with self._bake_lock:
                    self._baking = False
                self._status = f"Could not rebuild the shifted audio: {exc}"
                return
            if not self._swap_in(wav, target, gen):
                with self._bake_lock:
                    self._baking = False
                return

    def _swap_in(self, wav: Path, key: tuple[int, bool], gen: int) -> bool:
        """Attach ``wav``, drop the previous track, and hold the song position.

        Returns False when the caller should stop looping. Reloading an
        external track restarts it, so the master position is recomputed: the
        song moment being listened to must stay put while the video underneath
        it moves.
        """
        song_pos = self.song_position_s()
        old = self._tracks.external
        try:
            before = audio_track_ids(self._get("track-list"))
            self._player.command("audio-add", str(wav), "select")
            # Same race as at startup: wait for the new id before touching the
            # old one, or the removal can land while the replacement is still
            # arriving and leave no song track at all.
            added, tracks = self._await_added_track(self._player, before)
            if old is not None and old != added:
                # Remove afterwards, not before: dropping the only audio track
                # first makes mpv fall back to the video's own, which is an
                # audible blip in the wrong ear.
                self._player.command("audio-remove", str(old))
                tracks = self._get("track-list") or tracks
        except PreviewError as exc:
            # The old track is still attached and playing, so this is a
            # recoverable miss: the offset rides on the delay until next time.
            self._status = f"Kept the previous audio — {exc}"
            return False
        except Exception as exc:
            self._status = f"Could not swap the shifted audio: {exc}"
            return False

        with self._bake_lock:
            if gen != self._bake_gen:
                return False  # stopped while the swap was in flight
            self._baked_shift_ms, self._baked_split = key
            self._tracks = AudioTracks.from_track_list(tracks, external_id=added)
        self._apply_delay()
        self._apply_routing()
        if song_pos is not None:
            self.seek_song(song_pos)
        return True

    def _write_bake(self, shift_ms: int, split: bool) -> Path:
        """Write the mixed WAV. Blocking, and always off the UI thread."""
        with self._bake_lock:
            self._bake_parity ^= 1
            out = self._tmpdir / f"song-{self._bake_parity}.wav"
        return self._pcm.write_shifted_wav(
            out, shift_ms, video_pcm=self._video_pcm, split=split,
        )

    # ── Routing ──────────────────────────────────────────────────────────────

    def set_split(self, split: bool) -> None:
        split = bool(split)
        if split == self._split:
            return
        self._split = split
        if split and not self._can_split:
            self._status = "The video has no audio track, so split stereo is unavailable."
            return
        self._status = ""
        # The ears are mixed into the audio itself, so this is a rebuild rather
        # than a filter change.
        self._request_bake()

    def _apply_routing(self) -> None:
        """Play our mixed track and nothing else.

        There is no filter graph any more: the baked WAV already *is* what
        should be heard, ears and all. So the only routing left is selecting it
        and making sure the video's own audio track stays out of the way — in
        split mode its audio is present in the mix already, and hearing it
        twice, once unshifted, was its own kind of wrong.
        """
        if self._player is None:
            return
        song_aid = self._tracks.external
        if song_aid is None:
            self._status = "The song track did not attach — nothing to preview."
            return
        self._set("aid", song_aid)
        self._set("mute", False)

    # ── Position ─────────────────────────────────────────────────────────────

    def song_position_s(self) -> float | None:
        """Song moment currently audible.

        ``time_pos`` is master (video) time; the offset converts. The baked
        shift and the residual delay both cancel out of this, which is the
        arithmetic check that the two halves of the shift agree.
        """
        pos = self._get("time_pos")
        if pos is None:
            return None
        return song_from_master(float(pos), self._offset_ms)

    def seek_song(self, song_time_s: float) -> None:
        """Jump so ``song_time_s`` is audible, clamping at the video's start."""
        self._seek_master(master_from_song(song_time_s, self._offset_ms))

    def _seek_master(self, master_time_s: float) -> None:
        self._set("time_pos", max(0.0, master_time_s))

    def set_volume(self, level: int) -> None:
        """Live volume change.

        Used by the A/B harness to run both engines at once and swap which one
        is audible, since a gap between them would hide exactly the continuity
        difference being judged.
        """
        self._volume = int(level)
        self._set("volume", self._volume)

    def measured_drift_s(self) -> float | None:
        """mpv's own audio-vs-video difference, in seconds.

        This used to return a flat 0.0 on the grounds that one clock cannot
        drift against itself. That was true and beside the point: a single
        clock rules out *accumulating* divergence, not a *constant* offset
        introduced when the two streams were positioned — a keyframe-rounded
        seek being exactly that. Reporting what mpv measures is more use than
        asserting what ought to be true.
        """
        if self._player is None:
            return None
        avsync = self._get("avsync")
        try:
            return float(avsync)
        except (TypeError, ValueError):
            return None

    @property
    def seek_error_s(self) -> float:
        """How far the opening seek missed by. Negative means it landed early."""
        return self._seek_error_s

    def diagnostics(self) -> str:
        """One line of everything worth knowing when the sync looks wrong."""
        return "  ".join((
            f"baked {self._baked_shift_ms:+d}ms",
            f"unbaked {self.residual_delay_ms:+d}ms",
            f"delay {'live' if self._delay_is_live else 'DEAD'}",
            f"seek err {self._seek_error_s * 1000:+.0f}ms",
            f"avsync {self.measured_drift_s()}",
            f"aid v={self._tracks.internal} song={self._tracks.external}",
            f"split {'on' if self._split else 'off'}",
        ))

    def poll(self) -> None:
        """Nothing to do but notice the end. Deliberately: the two-instance
        engine's equivalent is a drift watchdog, and the absence here is the
        point of the comparison."""

    @property
    def ended(self) -> bool:
        return self._ended

    def _on_eof(self, _name, value) -> None:
        # mpv's event thread; a plain bool store is atomic under the GIL, the
        # same handling MediaPlayer._on_eof_reached uses.
        if value:
            self._ended = True

    # ── mpv plumbing ─────────────────────────────────────────────────────────

    def _get(self, name: str):
        return prop_get(self._player, name)

    def _set(self, name: str, value) -> bool:
        return prop_set(self._player, name, value)

    def _cleanup_tmp(self) -> None:
        tmp, self._tmpdir = self._tmpdir, None
        if tmp is not None:
            # Best-effort: on Windows mpv may still hold the WAV open for a
            # moment after terminate(), and a leftover temp dir is not worth
            # raising over.
            shutil.rmtree(tmp, ignore_errors=True)


class TwoInstancePreview:
    """The older design: two libmpv instances held together by a watchdog.

    Two players rather than one file with an external audio track, because a
    Cinema offset can be tens of seconds and mpv's ``audio-delay`` doesn't
    handle that gracefully on a single instance. Separate instances seek
    independently and are pulled back into line on a timer.

    Retained as the fallback for builds where the single-instance engine can't
    start, and as the reference the A/B harness compares against. The best it
    can promise is a *bound* on the error — see this module's header for why
    that bound is audible in split stereo.
    """

    def __init__(self, audio_path: Path, video_path: Path, *,
                 wid: int | None = None, volume: int | None = None):
        self._audio_path = Path(audio_path)
        self._video_path = Path(video_path)
        self._wid = wid
        self._volume = volume

        self._mpv_audio = None
        self._mpv_video = None
        self._offset_ms = 0
        self._split = False
        self._video_speed = 1.0
        self._last_error_s: float | None = None
        self._status = ""

    @property
    def is_running(self) -> bool:
        return self._mpv_audio is not None

    @property
    def status(self) -> str:
        return self._status

    def start(self, at_song_s: float, offset_ms: int, *, split: bool = False) -> None:
        mpv_mod = load_mpv()
        if mpv_mod is None:
            raise PreviewError("libmpv is not available")
        self.stop()
        self._offset_ms = int(offset_ms)
        self._split = bool(split)

        common = dict(
            idle="yes", osd_level=0,
            input_default_bindings=False, input_vo_keyboard=False,
        )
        try:
            self._mpv_audio = mpv_mod.MPV(vid="no", **common)
            if self._volume is not None:
                self._mpv_audio.volume = self._volume
            self._mpv_audio.loadfile(str(self._audio_path), start=f"{at_song_s:.3f}")
        except Exception as exc:
            self.stop()
            raise PreviewError(f"could not start audio preview: {exc}")

        try:
            video_kwargs = dict(
                mute="yes", keep_open="yes", input_cursor=False,
                cursor_autohide="no", **common,
            )
            if self._wid is not None:
                video_kwargs["wid"] = str(self._wid)
            self._mpv_video = mpv_mod.MPV(**video_kwargs)
            if self._volume is not None:
                self._mpv_video.volume = self._volume
            video_pos = max(0.0, master_from_song(at_song_s, self._offset_ms))
            self._mpv_video.loadfile(str(self._video_path), start=f"{video_pos:.3f}")
        except Exception:
            self._mpv_video = None  # audio-only preview is still useful
        self._apply_routing()

    def stop(self) -> None:
        self._video_speed = 1.0
        self._last_error_s = None
        for attr in ("_mpv_audio", "_mpv_video"):
            player = getattr(self, attr, None)
            setattr(self, attr, None)
            if player is not None:
                try:
                    player.terminate()
                except Exception:
                    pass

    def set_offset_ms(self, offset_ms: int, *, settling: bool = False) -> None:
        self._offset_ms = int(offset_ms)
        if settling or self._mpv_video is None:
            # Seeking on every motion event would stutter, so the watchdog
            # carries the video until the drag ends.
            return
        # Deliberately not skipped when the value is unchanged: a drag ends on
        # the value its last motion event set, and this is the resync that
        # settles it. Here the watchdog would have caught it within a tick
        # anyway; in the single-instance engine, which has no watchdog, the
        # same shape meant a drag never took effect at all.
        self._set_video_speed(1.0)
        pos = self.song_position_s()
        if pos is not None:
            self._seek_video(master_from_song(pos, self._offset_ms))

    def set_split(self, split: bool) -> None:
        split = bool(split)
        if split == self._split:
            return
        self._split = split
        self._apply_routing()

    def _apply_routing(self) -> None:
        """Route each player to its own ear, or back to normal.

        With the split off the video is simply muted and the song plays centred,
        which is the watch-it-and-see mode. The ear assignment when it's on is
        not arbitrary: Cinema's README tells users "sound from the video will
        play in your left ear, the map in your right ear", and reversing it
        would invert advice they already have.
        """
        video_ok = True
        if self._mpv_video is not None:
            try:
                self._mpv_video.af = pan_filter(1.0, EAR_BLEED) if self._split else ""
                self._mpv_video.mute = not self._split
            except Exception:
                video_ok = False
        if self._mpv_audio is not None:
            try:
                self._mpv_audio.af = pan_filter(EAR_BLEED, 1.0) if self._split else ""
            except Exception:
                pass  # song keeps playing centred; still usable
        self._status = ("" if video_ok else
                        "Could not route the video's audio — is there an audio track?")

    def song_position_s(self) -> float | None:
        if self._mpv_audio is None:
            return None
        try:
            pos = self._mpv_audio.time_pos
        except Exception:
            return None
        return None if pos is None else float(pos)

    def seek_song(self, song_time_s: float) -> None:
        if self._mpv_audio is not None:
            try:
                self._mpv_audio.time_pos = max(0.0, song_time_s)
            except Exception:
                pass
        self._seek_video(master_from_song(song_time_s, self._offset_ms))

    def set_volume(self, level: int) -> None:
        """Live volume change on whichever players exist — see the note on the
        single-instance engine's version."""
        self._volume = int(level)
        for player in (self._mpv_audio, self._mpv_video):
            if player is None:
                continue
            try:
                player.volume = self._volume
            except Exception:
                pass

    def measured_drift_s(self) -> float | None:
        """Video position minus where the offset says it should be.

        The number the single-instance engine makes unnecessary. Signed, so the
        harness can show which ear is early.
        """
        if self._mpv_video is None:
            return None
        song_pos = self.song_position_s()
        if song_pos is None:
            return None
        try:
            actual = self._mpv_video.time_pos
        except Exception:
            return None
        if actual is None:
            return None
        return float(actual) - master_from_song(song_pos, self._offset_ms)

    def poll(self) -> None:
        """The drift watchdog, at whatever rate the caller ticks.

        Holds the two players in the relationship the current offset states.
        They have independent clocks, so they drift; left uncorrected that
        would make split-stereo listening meaningless — you'd be judging the
        drift rather than the offset.
        """
        error = self.measured_drift_s()
        self._last_error_s = error
        if error is None:
            return
        song_pos = self.song_position_s()
        if song_pos is None:
            return
        expected = master_from_song(song_pos, self._offset_ms)
        if expected < 0:
            self._set_video_speed(1.0)
            return
        tolerance = DRIFT_TOLERANCE_SPLIT_S if self._split else DRIFT_TOLERANCE_S
        if abs(error) <= tolerance:
            self._set_video_speed(1.0)
            return
        if abs(error) > DRIFT_SEEK_THRESHOLD_S or not self._split:
            # Watching: a seek is instant and the click doesn't matter, since
            # the video's own audio is muted anyway.
            self._set_video_speed(1.0)
            self._seek_video(expected)
            return
        # Split stereo: a seek would be an obvious click in one ear, so ease
        # the error out by trimming playback speed instead.
        self._set_video_speed(drift_speed_trim(error))

    @property
    def video_speed(self) -> float:
        """Current speed trim — non-1.0 means a correction is audibly in flight."""
        return self._video_speed

    def _set_video_speed(self, speed: float) -> None:
        if self._mpv_video is None or self._video_speed == speed:
            return
        try:
            self._mpv_video.speed = speed
            self._video_speed = speed
        except Exception:
            pass

    def _seek_video(self, video_pos: float) -> None:
        if self._mpv_video is None:
            return
        try:
            self._mpv_video.time_pos = max(0.0, video_pos)
        except Exception:
            pass


# ── Engine selection ─────────────────────────────────────────────────────────


def start_preview(audio_path: Path, video_path: Path, at_song_s: float,
                  offset_ms: int, *, split: bool = False,
                  wid: int | None = None, volume: int | None = None,
                  prefer: str | None = None):
    """Start the preferred engine, falling back if it can't run.

    Returns ``(engine, note)``, where ``note`` is empty on the happy path and
    otherwise explains — in the user's terms, since it goes straight to the
    editor's status line — why the preview isn't the one they asked for.

    Blocking: decoding the song and waiting for libmpv to demux the video both
    take long enough to freeze a UI thread. Call it on a worker.

    The fallback exists because the single-instance engine asks more of libmpv
    than the older one does (decoding both sources up front, and attaching an
    external audio track), and a build that will not do those things should
    degrade to a working preview rather than to none — the same shape as
    ``VisualizerWindow._start_stream`` dropping from video to spectrum.

    Raises ``PreviewError`` only if *both* engines fail, since at that point
    there is nothing to degrade to.
    """
    from libraries import app_config

    audio_path, video_path = Path(audio_path), Path(video_path)
    prefer = prefer or app_config.get_preview_engine()

    if prefer == "single":
        engine = None
        try:
            pcm = PcmSource.decode(audio_path)
            try:
                # The video's audio is only needed for split stereo, so a video
                # without one is not a failure — it just can't be compared by
                # ear, which the engine reports if the split is switched on.
                video_pcm = PcmSource.decode(video_path).pcm
            except PreviewError:
                video_pcm = None
            engine = SinglePlayerPreview(video_path, pcm, video_pcm=video_pcm,
                                         wid=wid, volume=volume)
            engine.start(at_song_s, offset_ms, split=split)
            return engine, ""
        except Exception as exc:
            # Deliberately broad. PreviewError is the expected way for this
            # engine to decline, but a plain bug in it — an AttributeError, a
            # bad mpv interaction — must still leave the user with a working
            # preview rather than an exception on a worker thread and a button
            # stuck on "Preparing…". The fallback is the whole reason the older
            # engine is still here.
            if engine is not None:
                try:
                    engine.stop()  # release the half-built player and its temp files
                except Exception:
                    pass
            detail = exc if isinstance(exc, PreviewError) else f"{type(exc).__name__}: {exc}"
            note = (f"Falling back to the two-player preview — {detail}. "
                    "Sync may drift slightly.")
    else:
        note = ""

    engine = TwoInstancePreview(audio_path, video_path, wid=wid, volume=volume)
    engine.start(at_song_s, offset_ms, split=split)  # PreviewError propagates
    return engine, note
