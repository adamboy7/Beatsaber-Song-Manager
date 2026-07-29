"""Waveform PNG rendering via ffmpeg's ``showwavespic``, with a disk cache.

``showwavespic`` draws a whole input (or, with ``-ss``/``-t``, a window of
one) to a single image in one pass, which is exactly what the Cinema offset
editor needs for its two tracks. Rendering is slow enough on a long video
that doing it on every open — or on every zoom step — would be felt, so
results are cached under the app-data folder keyed by everything that could
change the pixels: source path, its mtime and size, the time window, the
output size and the colour.

Everything here is blocking and safe to call off the Tk thread; nothing in
this module touches tkinter.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from libraries import app_config
from libraries.audio_utils import find_ffmpeg

CACHE_DIR_NAME = "waveform-cache"

# Bound the cache so a large library browsed over many sessions doesn't grow
# it without limit. Each PNG is tens of KB, so this is a few MB at worst.
_MAX_CACHED_FILES = 400


class WaveformError(RuntimeError):
    """ffmpeg was missing, failed, or produced nothing usable."""


def cache_dir() -> Path:
    d = app_config.app_data_dir() / CACHE_DIR_NAME
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def _fingerprint(source: Path) -> str:
    """Identify the *contents* of ``source`` cheaply.

    mtime alone is not enough — replacing a file while preserving its
    timestamp is exactly the case ``song_data._folder_fingerprint`` was
    burned by — so size goes in too.
    """
    try:
        st = source.stat()
        return f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return "0:0"


def cache_key(source: Path, start_s: float, duration_s: float,
              width: int, height: int, color: str) -> str:
    raw = "|".join((
        str(Path(source).resolve()),
        _fingerprint(Path(source)),
        f"{start_s:.3f}", f"{duration_s:.3f}",
        str(width), str(height), color,
    ))
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def _ffmpeg_color(color: str) -> str:
    """Translate a Tk colour to something showwavespic accepts.

    ffmpeg wants ``0xRRGGBB`` or a named colour; Tk hex strings are
    ``#RRGGBB``. A ``#`` also has to be escaped inside a filtergraph, so
    converting is simpler than quoting.
    """
    c = (color or "").strip()
    if c.startswith("#") and len(c) in (7, 9):
        return "0x" + c[1:]
    return c or "white"


def render(
    source: Path,
    *,
    start_s: float = 0.0,
    duration_s: float = 0.0,
    width: int = 1200,
    height: int = 120,
    color: str = "#c724b1",
    ffmpeg: str | None = None,
    timeout: int = 120,
) -> Path:
    """Render (or return a cached) waveform PNG for a window of ``source``.

    ``duration_s <= 0`` renders the whole file. Raises ``WaveformError`` on
    any failure — a missing ffmpeg, a non-zero exit, or an output file that
    never appeared.
    """
    source = Path(source)
    if not source.exists():
        raise WaveformError(f"{source.name} does not exist")

    start_s = max(0.0, float(start_s))
    duration_s = max(0.0, float(duration_s))
    width = max(1, int(width))
    height = max(1, int(height))

    out = cache_dir() / f"{cache_key(source, start_s, duration_s, width, height, color)}.png"
    if out.exists() and out.stat().st_size > 0:
        return out

    exe = ffmpeg or find_ffmpeg()
    if not exe:
        raise WaveformError("ffmpeg is not available")

    # aformat=…mono collapses stereo to a single trace: showwavespic would
    # otherwise stack one row per channel and halve the vertical resolution
    # of each. split_channels is off by default but the layout still drives
    # the drawing, so the downmix is the reliable way to get one band.
    graph = (
        f"[0:a]aformat=channel_layouts=mono,"
        f"showwavespic=s={width}x{height}:colors={_ffmpeg_color(color)}"
    )
    cmd = [exe, "-hide_banner", "-nostdin", "-v", "error"]
    if start_s > 0:
        # Before -i: input seeking, so ffmpeg doesn't decode from zero to
        # reach a window late in a four-minute video.
        cmd += ["-ss", f"{start_s:.3f}"]
    cmd += ["-i", str(source)]
    if duration_s > 0:
        cmd += ["-t", f"{duration_s:.3f}"]
    cmd += ["-filter_complex", graph, "-frames:v", "1", "-y", str(out)]

    creation_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=creation_flag,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        out.unlink(missing_ok=True)
        raise WaveformError(f"waveform render timed out after {timeout}s")
    except OSError as exc:
        raise WaveformError(f"could not run ffmpeg: {exc}")

    if result.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        err = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise WaveformError(err or f"ffmpeg exited {result.returncode}")

    prune_cache()
    return out


def prune_cache(max_files: int = _MAX_CACHED_FILES) -> int:
    """Drop the least-recently-modified PNGs above ``max_files``.

    Best-effort: a file another process is reading (or a permissions problem)
    is skipped rather than raised. Returns the number removed.
    """
    try:
        files = sorted(
            cache_dir().glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True
        )
    except OSError:
        return 0
    removed = 0
    for stale in files[max_files:]:
        try:
            stale.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def clear_cache() -> None:
    """Delete every cached waveform. Best-effort."""
    try:
        for png in cache_dir().glob("*.png"):
            try:
                png.unlink()
            except OSError:
                pass
    except OSError:
        pass
