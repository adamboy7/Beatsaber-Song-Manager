import json
import shutil
import subprocess
import sys
from pathlib import Path

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
_incomplete_ffmpeg_notified = False


def set_probe_fallback_notifier(cb) -> None:
    """Register the callback described above (or None to clear it)."""
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


def get_audio_duration(path: Path) -> float | None:
    """Return audio duration in seconds, read directly from the file header.

    Uses mutagen (pure Python, no sidecar binary) as the primary path, which
    covers all supported formats: Ogg Vorbis (.ogg/.egg), MP3, WAV and M4A.
    mutagen sniffs by content rather than extension, so an .egg (really Ogg)
    is detected correctly, so the common case needs no external binary.

    Falls back to ffprobe when mutagen can't parse the file, then to libmpv
    (already bundled for playback) when no ffprobe binary is present — mpv is
    last because spinning up an instance is the heaviest of the three.
    """
    try:
        from mutagen import File as MutagenFile
        mf = MutagenFile(str(path))
        if mf is not None and mf.info is not None:
            length = getattr(mf.info, "length", None)
            if length:
                return float(length)
    except Exception:
        pass
    dur = _ffprobe_duration(path)
    if dur is not None:
        return dur
    _notify_incomplete_ffmpeg()
    return _mpv_duration(path)


def _notify_incomplete_ffmpeg() -> None:
    """Fire the probe-fallback notifier once, if this is the incomplete-ffmpeg
    case (ffmpeg present, ffprobe missing) and a notifier is registered."""
    global _incomplete_ffmpeg_notified
    if _incomplete_ffmpeg_notified:
        return
    if find_ffprobe() is not None or find_ffmpeg() is None:
        return  # ffprobe is available, or no ffmpeg at all — not this case.
    cb = _probe_fallback_notifier
    if cb is None:
        return
    _incomplete_ffmpeg_notified = True
    try:
        cb()
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
