import json
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
    """Why a duration probe came back empty, for the UI to act on.

    ``reader_available`` is the important one: when it's False the pure-Python
    reader itself is missing, so *every* probe in the app will fail no matter
    what external tools are installed. That's a broken install, not a missing
    ffmpeg, and offering an ffmpeg reinstall for it just sends the user in
    circles.
    """

    reader_available: bool
    ffmpeg_found: bool
    ffprobe_found: bool


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
