import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from libraries import platform_utils


def _local_dir() -> Path:
    # When frozen by PyInstaller, look next to the EXE, not in the temp bundle.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent.parent


# Cache only *successful* lookups. A permanent cache (e.g. lru_cache) would pin
# a startup-time miss forever, so dropping ffmpeg.exe next to the app wouldn't be
# picked up until a restart. By caching only a found path, a miss keeps re-probing
# on each call and detects a newly added binary on the fly — no reload needed.
_ffmpeg_cache: str | None = None
_ffprobe_cache: str | None = None
_ffplay_cache: str | None = None

_probe_fallback_notifier = None  # type: ignore[var-annotated]
_probe_tools_notified = False

_reader_options: list | None = None
_reader_unavailable: str | None = None


class ProbeFailure(NamedTuple):
    """Why a duration probe came back empty, and what would fix it.

    ``reader_available`` is the one that changes the diagnosis: when it's False
    the pure-Python reader itself is missing, so *every* probe in the app fails
    before it reaches a fallback. That's an incomplete install rather than a
    missing ffmpeg, and it needs saying — but see ``ffprobe_would_help``, since
    ffprobe still recovers the feature either way.
    """

    reader_available: bool
    ffmpeg_found: bool
    ffprobe_found: bool

    @property
    def remedy(self) -> str:
        """Which prompt this failure calls for.

        ``"reader"`` — the built-in reader is missing; report that as the cause.
        ``"ffmpeg-repair"`` — an ffmpeg is installed but has no ffprobe.
        ``"ffmpeg-install"`` — no ffmpeg at all.
        """
        if not self.reader_available:
            return "reader"
        return "ffmpeg-repair" if self.ffmpeg_found else "ffmpeg-install"

    @property
    def ffprobe_would_help(self) -> bool:
        """Whether installing ffmpeg (for its ffprobe) would restore durations.

        True whenever ffprobe is absent — including when the built-in reader is
        broken, because ffprobe covers the same formats and so stands in for it
        completely. False once ffprobe is present: it's already in the chain and
        merely couldn't parse this particular file, so there's nothing to add.
        """
        return not self.ffprobe_found


def set_probe_fallback_notifier(cb) -> None:
    """Register the callback described above (or None to clear it).

    ``cb`` is called as ``cb(failure: ProbeFailure)``."""
    global _probe_fallback_notifier
    _probe_fallback_notifier = cb


def _resolve(name: str) -> str | None:
    """Locate a binary: app/script directory first, then the per-user app-data
    folder, then PATH.

    The local filename is platform-aware (``ffmpeg.exe`` on Windows,
    ``ffmpeg`` on Linux/macOS), matching the extension-less binaries dropped
    beside the app or fetched by the auto-downloader. Side-by-side installs win;
    the app-data folder is where the auto-downloader lands binaries when the app
    itself is in a read-only/frozen location.
    """
    from libraries import app_config
    exe = platform_utils.exe_name(name)
    for directory in (_local_dir(), app_config.app_data_dir()):
        candidate = directory / exe
        if candidate.exists():
            return str(candidate)
    return shutil.which(name)


def find_ffmpeg() -> str | None:
    """Return path to ffmpeg: checks app directory first, then PATH.

    Re-probes on every call until found, so an ffmpeg.exe placed beside the app
    after launch is detected without restarting.
    """
    global _ffmpeg_cache
    if _ffmpeg_cache is None:
        _ffmpeg_cache = _resolve("ffmpeg")
    return _ffmpeg_cache


def find_ffprobe() -> str | None:
    """Return path to ffprobe: checks app directory first, then PATH.

    Re-probes on every call until found, so an ffprobe.exe placed beside the app
    after launch is detected without restarting.
    """
    global _ffprobe_cache
    if _ffprobe_cache is None:
        _ffprobe_cache = _resolve("ffprobe")
    return _ffprobe_cache


def find_ffplay() -> str | None:
    """Return path to ffplay: checks app directory first, then PATH.

    ffplay ships in the same bundle as ffmpeg/ffprobe and is the audio-playback
    fallback used when libmpv is unavailable. Re-probes on every call until
    found, so an ffplay dropped beside the app after launch is picked up without
    restarting.
    """
    global _ffplay_cache
    if _ffplay_cache is None:
        _ffplay_cache = _resolve("ffplay")
    return _ffplay_cache


def _reader_formats() -> list | None:
    """The mutagen FileType classes we sniff with, or None if unimportable.

    Passing an explicit ``options`` list to ``mutagen.File`` matters more than it
    looks: called with ``options=None`` it imports *26* format modules inside the
    function body, so in a frozen build a single un-bundled format module makes
    every duration read raise ImportError — for every song, regardless of format.
    Naming only the four formats this app documents (Ogg Vorbis for .ogg/.egg,
    MP3, WAV, M4A) keeps that surface to exactly the modules listed as
    hiddenimports in Browser.spec / Browser.linux.spec, and skips 22 needless
    imports besides.
    """
    global _reader_options, _reader_unavailable
    if _reader_options is not None or _reader_unavailable is not None:
        return _reader_options
    try:
        from mutagen.mp3 import MP3
        from mutagen.mp4 import MP4
        from mutagen.oggvorbis import OggVorbis
        from mutagen.wave import WAVE
    except ImportError as e:
        _reader_unavailable = str(e)
        return None
    # Ogg first: it's what Beat Saber ships, so the usual case scores on the
    # first candidate.
    _reader_options = [OggVorbis, MP3, WAVE, MP4]
    return _reader_options


def reader_available() -> bool:
    """Whether the built-in (pure-Python) duration reader can be used at all."""
    return _reader_formats() is not None


def _reader_duration(path: Path) -> tuple[float | None, bool]:
    """Read the duration with mutagen.

    Returns ``(duration, reader_available)``. The second value separates "this
    file couldn't be parsed" from "the reader isn't installed" — conflating them
    is what used to make a missing mutagen look like a broken ffmpeg.
    """
    options = _reader_formats()
    if options is None:
        return None, False
    try:
        from mutagen import File as MutagenFile
        mf = MutagenFile(str(path), options=options)
        if mf is not None and mf.info is not None:
            length = getattr(mf.info, "length", None)
            if length:
                return float(length), True
    except ImportError:
        return None, False  # partially-bundled mutagen — same class of problem
    except Exception:
        pass  # unparseable file; the external fallbacks may still manage it
    return None, True


def get_audio_duration(path: Path) -> float | None:
    """Return audio duration in seconds, read directly from the file header.

    Uses mutagen (pure Python, no sidecar binary) as the primary path, which
    covers all supported formats: Ogg Vorbis (.ogg/.egg), MP3, WAV and M4A.
    mutagen sniffs by content rather than extension, so an .egg (really Ogg)
    is detected correctly, so the common case needs no external binary.

    Falls back to ffprobe when mutagen can't parse the file, then to libmpv
    (already bundled for playback) when ffprobe doesn't yield a value — mpv is
    last because spinning up an instance is the heaviest of the three. If every
    probe comes up empty, a one-per-run UI hook is fired with a ``ProbeFailure``
    describing why (see ``set_probe_fallback_notifier``).
    """
    dur, have_reader = _reader_duration(path)
    if dur is not None:
        return dur
    # The reader couldn't get it — try ffprobe, then libmpv. Each returns None
    # when its tool is missing or can't parse the file.
    dur = _ffprobe_duration(path)
    if dur is not None:
        return dur
    dur = _mpv_duration(path)
    if dur is not None:
        return dur
    _notify_missing_probe_tools(have_reader)
    return None


def _notify_missing_probe_tools(have_reader: bool = True) -> None:
    """Fire the probe-fallback notifier once, so the UI can offer a real fix.

    Two distinct situations reach here, and the callback is told which:

    * ``reader_available=False`` — mutagen is missing or partly bundled. Nothing
      about ffmpeg will fix this, so the UI must not offer an ffmpeg reinstall.
    * ``reader_available=True`` — the reader just couldn't parse this file and
      both external fallbacks are absent, which is the case an ffprobe (i.e.
      ffmpeg) install genuinely helps with.

    No-op when a fallback tool is present (the chain isn't starved) or no
    notifier is registered.
    """
    global _probe_tools_notified
    if _probe_tools_notified:
        return
    if have_reader:
        if find_ffprobe() is not None:
            return  # ffprobe present — the chain isn't starved of tools.
        try:
            from libraries import mpv_backend
            if mpv_backend.load_mpv() is not None:
                return  # libmpv is a viable alternative — no prompt needed.
        except Exception:
            pass  # treat an unloadable mpv as missing
    cb = _probe_fallback_notifier
    if cb is None:
        return
    _probe_tools_notified = True
    try:
        cb(ProbeFailure(
            reader_available=have_reader,
            ffmpeg_found=find_ffmpeg() is not None,
            ffprobe_found=find_ffprobe() is not None,
        ))
    except Exception:
        pass


def _ffprobe_duration(path: Path) -> float | None:
    """Fallback duration probe using ffprobe, or None if unavailable."""
    ffprobe = find_ffprobe()
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams", str(path)],
            capture_output=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        data = json.loads(result.stdout.decode("utf-8", errors="replace"))
        for stream in data.get("streams", []):
            dur = stream.get("duration")
            if dur:
                return float(dur)
    except Exception:
        pass
    return None


# ── First-sound detection ────────────────────────────────────────────────────
#
# Used by the Cinema offset editor to line the video up by where the music
# starts in each file. ffmpeg's `silencedetect` filter reports silent *runs* on
# stderr; the one we want is a run that begins at the very start of the file,
# whose end is therefore the first sound.

# -40 dBFS rather than a stricter figure: Beat Saber's audio is very often a
# quiet room-tone or encoder lead-in rather than digital silence, and a −55 dB
# fade-in is inaudible but would defeat a threshold near zero.
SILENCE_NOISE_DB = -40.0

# A silent run shorter than this isn't reported at all, so it's also the
# shortest lead-in that can be detected. Below ~50 ms the "silence" is more
# likely a codec artefact than something a mapper put there.
SILENCE_MIN_S = 0.05

# How close to t=0 a silent run must begin to count as *leading* silence. It is
# not always exactly 0 — a lossy decoder can report the run starting a frame
# in — and anything later is a gap inside the track, not a lead-in.
LEADING_SILENCE_TOLERANCE_S = 0.05

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")


class OnsetError(RuntimeError):
    """ffmpeg was missing, failed, or the file has no audio to analyse."""


def parse_first_sound_s(log: str) -> float | None:
    """Time of the first sound, from a ``silencedetect`` stderr log.

    Four cases, and the difference between them is the whole point:

    * No silence reported at all — the file is audible from its first sample,
      so the answer is ``0.0``.
    * The first silent run starts *after* the tolerance — that's a gap inside
      the track (a breakdown, a bar of rest), not a lead-in, and the file is
      again audible from ``0.0``. Reading it as a lead-in would be badly wrong:
      a song with a silent bar at 0:30 would report a 30-second lead-in.
    * A run starting at ~0 with an end — that end is the first sound.
    * A run starting at ~0 that never ends — ffmpeg flushes ``silence_end`` at
      EOF, so a genuinely silent file does produce an end; a log truncated
      before it does not. ``None`` either way: no sound was found.
    """
    start_match = _SILENCE_START_RE.search(log)
    if start_match is None:
        return 0.0
    try:
        first_start = float(start_match.group(1))
    except ValueError:
        return 0.0
    if first_start > LEADING_SILENCE_TOLERANCE_S:
        return 0.0  # a gap mid-track, not a lead-in
    end_match = _SILENCE_END_RE.search(log, start_match.end())
    if end_match is None:
        return None
    try:
        return max(0.0, float(end_match.group(1)))
    except ValueError:
        return None


def first_sound_offset_s(
    path: Path,
    *,
    ffmpeg: str | None = None,
    noise_db: float = SILENCE_NOISE_DB,
    min_silence_s: float = SILENCE_MIN_S,
    duration_s: float | None = None,
    timeout: int = 120,
) -> float | None:
    """Seconds of silence before the first sound in ``path``.

    Works on video as well as audio — ``-vn`` drops the video stream, which is
    not a detail: it takes a four-minute 640×360 clip from ~1.0 s to ~0.15 s,
    since only the audio ever needed decoding.

    Returns ``None`` when the file is silent throughout. That case is otherwise
    indistinguishable from a real onset: ffmpeg flushes the open silent run at
    EOF, so an entirely silent file reports its own duration as the "first
    sound". Passing ``duration_s`` lets that be caught; without it, a silent
    file returns a value equal to its length.

    Raises ``OnsetError`` if ffmpeg is missing, fails, or the input has no
    audio stream at all (a silent video — ffmpeg exits non-zero because the
    filter graph has nothing to feed it).
    """
    path = Path(path)
    if not path.exists():
        raise OnsetError(f"{path.name} does not exist")

    exe = ffmpeg or find_ffmpeg()
    if not exe:
        raise OnsetError("ffmpeg is not available")

    # `-v info`, unlike the `-v error` used everywhere else here: silencedetect
    # reports its findings *as info-level log lines*, so quieting ffmpeg the
    # usual way discards the entire result and every file looks like it starts
    # with sound. `-nostats` trims the progress spam that comes with it.
    cmd = [
        exe, "-hide_banner", "-nostdin", "-nostats", "-v", "info",
        "-i", str(path),
        "-vn",
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence_s}",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise OnsetError(f"silence detection timed out after {timeout}s")
    except OSError as exc:
        raise OnsetError(f"could not run ffmpeg: {exc}")

    log = (result.stderr or b"").decode("utf-8", errors="replace")
    if result.returncode != 0:
        if "does not contain any stream" in log:
            raise OnsetError(f"{path.name} has no audio track")
        raise OnsetError(log.strip().splitlines()[-1] if log.strip()
                         else f"ffmpeg exited {result.returncode}")

    onset = parse_first_sound_s(log)
    if onset is None:
        return None
    if duration_s and onset >= duration_s - 0.05:
        return None  # silent to the end; the "onset" is just EOF
    return onset


def _mpv_duration(path: Path) -> float | None:
    """Last-resort duration probe via libmpv, or None if unavailable.

    Reuses the libmpv already bundled for playback (loaded through
    mpv_backend), so no additional binary is required. Loads the file paused
    with audio and video output disabled, reads the parsed ``duration``
    property, then tears the instance down. Imported lazily to avoid a circular
    import — mpv_backend imports from this module.
    """
    from libraries import mpv_backend
    mpv = mpv_backend.load_mpv()
    if mpv is None:
        return None
    player = None
    try:
        player = mpv.MPV(pause=True, idle=True, video=False,
                         ao="null", vo="null", mute=True)
        player.play(str(path))
        # Block until libmpv has demuxed the header and populated duration.
        dur = player.wait_for_property("duration", timeout=5)
        if dur:
            return float(dur)
    except Exception:
        pass
    finally:
        if player is not None:
            try:
                player.terminate()
            except Exception:
                pass
    return None
