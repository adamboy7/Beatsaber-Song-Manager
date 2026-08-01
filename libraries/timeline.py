"""Geometry shared by the waveform-alignment editors.

Two windows now draw one media file against another on a common time axis and
let the user slide one of them: the Cinema video offset editor
(``cinema_offset_window``) and the audio replacement editor
(``audio_replace_window``). The parts that don't depend on *what* is being
aligned live here — window clamping for ffmpeg, view scrolling, and the
pixel/millisecond conversion a drag goes through.

What deliberately stays out is every function whose **sign** encodes a
convention. Cinema's offset means "starts the video earlier", which inverts the
drag direction (``cinema_offset_window.drag_to_offset_ms``); the audio editor's
offset means "the replacement begins this far into the song", which doesn't
(``audio_replace_window.drag_to_offset_ms``). Sharing ``drag_delta_ms`` and
letting each caller apply its own sign keeps that difference visible at the two
call sites instead of hiding it behind a flag.

Free functions with no Tk and no I/O: this is the part worth testing, and the
test suite never opens a window.
"""

from __future__ import annotations


def fmt_time(seconds: float) -> str:
    """``m:ss``, clamped at zero."""
    seconds = max(0.0, seconds)
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def clamp_window(start_s: float, length_s: float,
                 media_duration_s: float) -> tuple[float, float] | None:
    """Intersect ``[start, start+length]`` with ``[0, duration]``.

    ffmpeg can't seek before zero or past the end, so a window running off
    either edge is trimmed; the caller positions the shorter render by the
    returned start. ``None`` means no overlap at all — which happens
    legitimately at large offsets, where the visible song range maps entirely
    outside the other file.
    """
    if media_duration_s <= 0 or length_s <= 0:
        return None
    lo = max(0.0, start_s)
    hi = min(media_duration_s, start_s + length_s)
    if hi - lo <= 0.01:
        return None
    return lo, hi - lo


def clamp_view_start(center_s: float, span_s: float, duration_s: float) -> float:
    """Left edge of a ``span_s`` window centred on ``center_s``, kept in range."""
    return max(0.0, min(max(0.0, duration_s - span_s), center_s - span_s / 2))


def song_time_to_x(song_time_s: float, view_start_s: float, px_per_s: float) -> float:
    """Canvas x of a moment on the song's timeline."""
    return (song_time_s - view_start_s) * px_per_s


def x_to_song_time(x_px: float, view_start_s: float, px_per_s: float) -> float:
    """Inverse of :func:`song_time_to_x`; ``px_per_s <= 0`` pins to the view start."""
    if px_per_s <= 0:
        return view_start_s
    return view_start_s + x_px / px_per_s


def drag_delta_ms(dx_px: float, px_per_s: float) -> float:
    """Milliseconds spanned by a horizontal drag of ``dx_px``.

    Unsigned in the sense that matters: it reports how far the pointer moved in
    time, and leaves it to the caller to decide whether moving a strip right
    raises or lowers its offset. Returns a float so a caller accumulating
    several drags doesn't round at every step.
    """
    if px_per_s <= 0:
        return 0.0
    return (dx_px / px_per_s) * 1000.0
