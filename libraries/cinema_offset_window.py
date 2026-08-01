"""Cinema video offset editor.

Shows the song's waveform (fixed) above the Cinema video's audio waveform
(draggable) on a shared song-time axis, and writes the resulting offset back
to ``cinema-video.json``.

Anything below attributed to Cinema was read out of the mod's own source and
documentation, at https://github.com/Kevga/BeatSaberCinema — file paths are
relative to that repository.

Timeline model
--------------
Everything on screen is drawn against **song time**. Cinema's offset maps the
two timelines::

    video_time = song_time + offset_ms / 1000

which is the same relation ``VisualizerWindow._video_pos`` uses, and which the
mod's own UI confirms: ``BeatSaberCinema/VideoMenu/Views/video-menu.bsml``
labels its ``+20 ms`` button "Starts video earlier". So a *positive* offset
slides the video's content *left* on this window's axis, and dragging the
video strip left increases the offset. The nudge steps (20 / 100 / 1000 ms)
are Cinema's own, so muscle memory carries over from the in-game menu.

Preview
-------
Delegated to ``cinema_preview``, which offers two engines: one libmpv instance
playing a single audio track that the module mixes itself — the song shifted by
the offset, and in split stereo the video's audio panned into the other ear —
or the older pair of separate players held together by a drift watchdog.
``start_preview`` picks between them and falls back on its own, so nothing here
needs to know which one it got.

The "Split stereo" checkbox is the mod's own sync technique, quoting its
README: "Sound from the video will play in your left ear, the map in your
right ear. If the sound from the left ear is behind, use the '+' buttons."

Rendering
---------
Waveforms are ffmpeg ``showwavespic`` PNGs (see ``waveform``), rendered off
the Tk thread and cached on disk. Re-rendering on every nudge would be far
too slow, so the video strip is rendered over a window *wider* than the
visible span (``_PAD_FRAC`` on each side) and simply repositioned on the
canvas as the offset changes; a re-render is only queued once the offset has
moved far enough to expose an edge.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

import tkinter as tk
from PIL import Image, ImageTk

from libraries import app_config, cinema_video, dialogs, timeline, waveform
from libraries.audio_utils import (
    OnsetError,
    find_ffmpeg,
    first_sound_offset_s,
    get_audio_duration,
)
from libraries.cinema_preview import PreviewError, start_preview
from libraries.constants import ACCENT_COLOR, SUBTEXT_COLOR, TEXT_COLOR
from libraries.mpv_backend import load_mpv

from libraries.cinema_preview import (  # noqa: F401  (re-export)
    DRIFT_MAX_SPEED_TRIM as _DRIFT_MAX_SPEED_TRIM,
    DRIFT_SEEK_THRESHOLD_S as _DRIFT_SEEK_THRESHOLD_S,
    DRIFT_TOLERANCE_S as _DRIFT_TOLERANCE_S,
    DRIFT_TOLERANCE_SPLIT_S as _DRIFT_TOLERANCE_SPLIT_S,
    EAR_BLEED as _EAR_BLEED,
    drift_speed_trim,
    pan_filter,
)

if TYPE_CHECKING:
    from Browser import SongBrowser
    from libraries.song_data import SongInfo

# ── Palette ──────────────────────────────────────────────────────────────────
_BG = "#0d0d1a"
_STRIP_BG = "#141422"
_GRID = "#2a2a3a"
_SONG_WAVE = "#4ec9ff"      # cyan: the fixed reference
_VIDEO_WAVE = ACCENT_COLOR  # magenta: the thing you move
_PLAYHEAD = "#ffd700"

# ── Geometry ─────────────────────────────────────────────────────────────────
_CANVAS_W = 900
_OVERVIEW_H = 54
_STRIP_H = 104
_PREVIEW_W, _PREVIEW_H = 384, 216

# Fraction of the visible span rendered beyond each edge of the video strip,
# giving the user room to drag before a re-render is needed.
_PAD_FRAC = 0.6

# Cinema's own offset steps, from the in-game menu's six nudge buttons
# (VideoMenu.cs, {De,In}creaseOffset{Low,Mid,High}).
_STEPS = (20, 100, 1000)

# Zoom presets: (label, span in seconds). None means "the whole song".
_ZOOMS: tuple[tuple[str, float | None], ...] = (
    ("Whole song", None),
    ("60 s", 60.0),
    ("30 s", 30.0),
    ("10 s", 10.0),
    ("4 s", 4.0),
    ("2 s", 2.0),
)
_DEFAULT_ZOOM = 3  # 10 s — tight enough that a 20 ms nudge is ~2 px


_fmt_time = timeline.fmt_time


# ── Timeline math ────────────────────────────────────────────────────────────
# Free functions rather than methods: this is the part worth testing, and the
# test suite never opens a window.

clamp_window = timeline.clamp_window
clamp_view_start = timeline.clamp_view_start
song_time_to_x = timeline.song_time_to_x


def drag_to_offset_ms(base_offset_ms: int, dx_px: float, px_per_s: float) -> int:
    """Offset after dragging the video strip ``dx_px`` from ``base_offset_ms``.

    Dragging left (negative dx) shows the video's content earlier in song
    time, and "earlier" is what a larger offset means — hence the sign flip.
    The audio replacement editor subtracts nothing in the equivalent place: its
    offset is a plain "starts here", so its drag reads the other way round.
    """
    if px_per_s <= 0:
        return int(base_offset_ms)
    return int(round(base_offset_ms - timeline.drag_delta_ms(dx_px, px_per_s)))


def nudge_delta_ms(step_ms: int, direction: int) -> int:
    """Offset change for an arrow-key nudge.

    ``direction`` is the direction the *waveform* should move on screen: -1
    for Left, +1 for Right. That's the same inversion ``drag_to_offset_ms``
    applies — moving the video strip left shows its content earlier in song
    time, which is a larger offset — and the reason the keys are not simply
    ``±step``. Arrow keys manipulate the strip; the ``+``/``−`` buttons
    manipulate the number, matching Cinema's own menu. Those are opposite
    directions on purpose.
    """
    return -direction * step_ms


def video_time_to_x(video_time_s: float, offset_ms: int,
                    view_start_s: float, px_per_s: float) -> float:
    """Canvas x of a moment in the *video's* timeline, on the song-time axis.

    Inverting ``video_time = song_time + offset`` gives
    ``song_time = video_time - offset``, so raising the offset slides the
    video's content left — the mod's "starts video earlier".
    """
    return song_time_to_x(video_time_s - offset_ms / 1000.0, view_start_s, px_per_s)


def detected_offset_ms(song_onset_s: float, video_onset_s: float) -> int:
    """Offset that lines the two first-sounds up, in ms.

    "Detect Silence" assumes the same music starts in both files, and asks what
    offset makes those two moments the same instant. Substituting
    ``song_time = song_onset`` into ``video_time = song_time + offset`` and
    requiring ``video_time = video_onset`` gives the difference directly.

    So a video with a long intro before the music (video_onset large) needs a
    *positive* offset — Cinema's "starts video earlier" — which is exactly
    right: the video must be wound forward past its intro to meet the song.
    """
    return int(round((video_onset_s - song_onset_s) * 1000.0))


def _render_one(path, window, px_per_s: float, color: str, height: int,
                ffmpeg: str | None) -> Path | None:
    """Render one strip, or None if there's nothing (or ffmpeg fails).

    Runs on a worker thread — no Tk, no shared state.
    """
    if not path or window is None:
        return None
    start_s, length_s = window
    try:
        return waveform.render(
            Path(path), start_s=start_s, duration_s=length_s,
            width=max(1, int(round(length_s * px_per_s))), height=height,
            color=color, ffmpeg=ffmpeg,
        )
    except waveform.WaveformError:
        return None


class CinemaOffsetWindow(tk.Toplevel):
    """Editor for one song's Cinema video offset."""

    def __init__(self, browser: "SongBrowser", song: "SongInfo",
                 *, allow_backup: bool = True):
        super().__init__(browser)
        self._browser = browser
        self._song = song
        # False for a config this app just created from scratch: there is no
        # prior state to restore to, so a .bak would only preserve our own
        # unsynced offset-0 config. See save_offset.
        self._allow_backup = allow_backup

        self._original_offset_ms = int(song.cinema_video_offset_ms)
        self._offset_ms = self._original_offset_ms
        # The mapper's value, when Cinema recorded one before a previous
        # override — that, not the current value, is what "Reset" should
        # return to.
        self._mapper_offset_ms: int | None = None

        self._song_duration_s = 0.0
        self._video_duration_s = float(song.cinema_video_duration_s or 0)

        self._zoom_index = _DEFAULT_ZOOM
        self._view_start_s = 0.0

        # Rendered strips: PhotoImage refs must outlive the canvas item.
        self._photo_song: ImageTk.PhotoImage | None = None
        self._photo_video: ImageTk.PhotoImage | None = None
        self._photo_ov_song: ImageTk.PhotoImage | None = None
        self._photo_ov_video: ImageTk.PhotoImage | None = None
        # Video strip provenance: (video_time_start, video_time_length).
        self._video_strip_window: tuple[float, float] | None = None
        self._ov_video_px_per_s = 0.0
        self._render_gen = 0
        self._ov_render_gen = 0
        # Offset the video strip was rendered at, so we know how far it has
        # been dragged since and when the rendered margin runs out.
        self._render_offset_ms = self._offset_ms

        self._drag_origin: tuple[int, int] | None = None

        # Silence detection: one at a time, and its result held separately so a
        # render finishing afterwards can restore it rather than blank it.
        self._detecting = False
        self._detect_note = ""

        self._preview = None
        self._preview_starting = False
        self._preview_gen = 0
        # Last engine status shown, so _report_preview_status can write only on
        # a change rather than on every nudge.
        self._preview_note = ""
        self._preview_after_id: str | None = None
        self._preview_anchor_s = 0.0
        self._browser_playback_paused_by_us = False

        self.title(f"Cinema Offset — {song.display_name}")
        self.configure(bg=_BG)
        self.resizable(False, False)
        try:
            icon = tk.PhotoImage(file=Path(__file__).parent.parent / "Album.png")
            self.iconphoto(False, icon)
            self._icon = icon
        except Exception:
            pass  # a missing icon shouldn't stop the window opening

        self._build_ui()
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._load_original_offset()
        self._update_offset_display()
        self._probe_durations_async()

    # ── Construction ─────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        pad = dict(padx=14)

        header = tk.Frame(self, bg=_BG)
        header.pack(fill="x", pady=(12, 6), **pad)
        tk.Label(
            header, text=self._song.display_name, bg=_BG, fg=TEXT_COLOR,
            font=("Segoe UI", 11, "bold"), anchor="w",
        ).pack(side="left")
        self._status_lbl = tk.Label(
            header, text="Rendering waveforms…", bg=_BG, fg=SUBTEXT_COLOR,
            font=("Segoe UI", 9), anchor="e",
        )
        self._status_lbl.pack(side="right")

        # ── Overview ────────────────────────────────────────────────────────
        self._ov_canvas = tk.Canvas(
            self, width=_CANVAS_W, height=_OVERVIEW_H, bg=_STRIP_BG,
            highlightthickness=0, bd=0, cursor="hand2",
        )
        self._ov_canvas.pack(pady=(0, 8), **pad)
        self._ov_canvas.bind("<Button-1>", self._on_overview_click)
        self._ov_canvas.bind("<B1-Motion>", self._on_overview_click)

        # ── Detail strips ───────────────────────────────────────────────────
        self._song_canvas = self._make_strip("Song", _SONG_WAVE)
        self._video_canvas = self._make_strip("Video", _VIDEO_WAVE, draggable=True)

        ruler = tk.Frame(self, bg=_BG)
        ruler.pack(fill="x", **pad)
        self._ruler_left = tk.Label(ruler, text="", bg=_BG, fg=SUBTEXT_COLOR,
                                    font=("Segoe UI", 8))
        self._ruler_left.pack(side="left")
        self._ruler_mid = tk.Label(ruler, text="", bg=_BG, fg=_PLAYHEAD,
                                   font=("Segoe UI", 8))
        self._ruler_mid.pack(side="left", expand=True)
        self._ruler_right = tk.Label(ruler, text="", bg=_BG, fg=SUBTEXT_COLOR,
                                     font=("Segoe UI", 8))
        self._ruler_right.pack(side="right")

        # ── Offset controls ─────────────────────────────────────────────────
        controls = tk.Frame(self, bg=_BG)
        controls.pack(fill="x", pady=(10, 4), **pad)

        for step in reversed(_STEPS):
            self._nudge_button(controls, -step)
        self._offset_var = tk.StringVar(value="0")
        entry = dialogs.themed_entry(
            controls, textvariable=self._offset_var, width=8,
            justify="center", font=("Segoe UI", 11, "bold"),
        )
        entry.pack(side="left", padx=6)
        entry.bind("<Return>", self._on_entry_commit)
        entry.bind("<FocusOut>", self._on_entry_commit)
        self._offset_entry = entry
        tk.Label(controls, text="ms", bg=_BG, fg=SUBTEXT_COLOR,
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 8))
        for step in _STEPS:
            self._nudge_button(controls, step)

        self._zoom_var = tk.StringVar(value=_ZOOMS[self._zoom_index][0])
        zoom = dialogs.themed_option_menu(
            controls, textvariable=self._zoom_var,
            values=[label for label, _ in _ZOOMS], width=11,
            command=self._on_zoom_changed,
        )
        zoom.pack(side="right")
        tk.Label(controls, text="Zoom", bg=_BG, fg=SUBTEXT_COLOR,
                 font=("Segoe UI", 9)).pack(side="right", padx=(0, 6))

        tk.Label(
            self,
            text="Drag the video strip, or nudge it with ← / → "
                 "(Shift ×100 ms, Ctrl ×1000 ms). Positive offset starts the video earlier.",
            bg=_BG, fg=SUBTEXT_COLOR, font=("Segoe UI", 8), anchor="w",
        ).pack(fill="x", pady=(0, 8), **pad)

        # ── Preview ─────────────────────────────────────────────────────────
        preview = tk.Frame(self, bg=_BG)
        preview.pack(fill="x", pady=(0, 10), **pad)
        self._preview_canvas = tk.Canvas(
            preview, width=_PREVIEW_W, height=_PREVIEW_H, bg="#000000",
            highlightthickness=1, highlightbackground=_GRID, bd=0,
        )
        self._preview_canvas.pack(side="left")

        side = tk.Frame(preview, bg=_BG)
        side.pack(side="left", fill="both", expand=True, padx=(14, 0))
        self._preview_btn = dialogs.themed_button(
            side, "▶  Preview", self._toggle_preview, primary=True, width=12,
        )
        self._preview_btn.pack(anchor="w")

        self._split_var = tk.BooleanVar(value=app_config.get_split_preview())
        self._split_check = tk.Checkbutton(
            side, variable=self._split_var, command=self._on_split_toggled,
            text="Split stereo — video in your left ear, song in your right",
            bg=_BG, fg=TEXT_COLOR, activebackground=_BG, activeforeground=TEXT_COLOR,
            selectcolor=_STRIP_BG, highlightthickness=0, bd=0, anchor="w",
            font=("Segoe UI", 9), cursor="hand2", justify="left",
            wraplength=_CANVAS_W - _PREVIEW_W - 60,
        )
        self._split_check.pack(anchor="w", pady=(10, 0))

        self._preview_lbl = tk.Label(
            side, text="", bg=_BG, fg=SUBTEXT_COLOR, font=("Segoe UI", 8),
            wraplength=_CANVAS_W - _PREVIEW_W - 40, justify="left", anchor="nw",
        )
        self._preview_lbl.pack(anchor="w", pady=(8, 0), fill="x")
        self._update_preview_hint()

        # ── Buttons ─────────────────────────────────────────────────────────
        buttons = tk.Frame(self, bg=_BG)
        buttons.pack(fill="x", pady=(0, 14), **pad)
        dialogs.themed_button(buttons, "Save", self._save, primary=True).pack(side="right")
        dialogs.themed_button(buttons, "Cancel", self._on_close).pack(side="right", padx=(0, 8))
        self._reset_btn = dialogs.themed_button(buttons, "Reset", self._reset)
        self._reset_btn.pack(side="left")
        self._detect_btn = dialogs.themed_button(
            buttons, "Detect Silence", self._detect_silence,
        )
        self._detect_btn.pack(side="left", padx=(8, 0))

    def _make_strip(self, label: str, color: str, draggable: bool = False) -> tk.Canvas:
        row = tk.Frame(self, bg=_BG)
        row.pack(fill="x", padx=14, pady=(0, 4))
        tag = tk.Label(row, text=label, bg=_BG, fg=color, font=("Segoe UI", 8, "bold"),
                       anchor="w")
        tag.pack(anchor="w")
        canvas = tk.Canvas(
            row, width=_CANVAS_W, height=_STRIP_H, bg=_STRIP_BG,
            highlightthickness=0, bd=0,
            cursor="sb_h_double_arrow" if draggable else "arrow",
        )
        canvas.pack()
        if draggable:
            canvas.bind("<Button-1>", self._on_drag_start)
            canvas.bind("<B1-Motion>", self._on_drag_move)
            canvas.bind("<ButtonRelease-1>", self._on_drag_end)
        return canvas

    def _nudge_button(self, parent: tk.Frame, delta_ms: int) -> None:
        label = f"{delta_ms:+d}"
        dialogs.themed_button(
            parent, label, lambda d=delta_ms: self._nudge(d),
            padx=8, font=("Segoe UI", 8),
        ).pack(side="left", padx=1)

    def _bind_keys(self) -> None:
        self.bind("<Left>", lambda e: self._arrow(e, -1))
        self.bind("<Right>", lambda e: self._arrow(e, 1))
        self.bind("<space>", self._on_space)
        self.bind("<Escape>", lambda e: self._on_close())
        self.bind("<Control-s>", lambda e: self._save())

    def _typing_in_entry(self) -> bool:
        """True while the offset entry has focus.

        These shortcuts are bound on the toplevel, which fires *after* the
        Entry's own class bindings — so without this check, typing a number
        would also nudge the offset and space would insert a character on its
        way to toggling the preview.
        """
        try:
            return self.focus_get() is self._offset_entry
        except (tk.TclError, KeyError):
            return False

    def _on_space(self, _event: tk.Event) -> str | None:
        if self._typing_in_entry():
            return None
        self._toggle_preview()
        return "break"

    # ── Duration probing ─────────────────────────────────────────────────────

    def _probe_durations_async(self) -> None:
        """Measure both media lengths off the UI thread.

        ``cinema_video_duration_s`` comes from the manifest and is often 0 or
        simply absent, and the song has no duration on ``SongInfo`` at all, so
        both may need a real probe — which can take seconds on a cold cache.
        """
        song = self._song
        audio_path = song.audio_path
        video_path = song.cinema_video_path
        known_video = self._video_duration_s

        def _work():
            audio_len = get_audio_duration(Path(audio_path)) if audio_path else None
            video_len = known_video
            if video_len <= 0 and video_path:
                video_len = get_audio_duration(Path(video_path)) or 0.0
            self._dispatch(lambda: self._on_durations(audio_len or 0.0, video_len or 0.0))

        threading.Thread(target=_work, daemon=True).start()

    def _on_durations(self, song_len: float, video_len: float) -> None:
        if not self._alive():
            return
        self._song_duration_s = song_len
        self._video_duration_s = video_len
        if song_len <= 0:
            self._set_status("Could not measure the song's length.")
            return
        self._center_view_on(0.0)
        self._request_overview_render()
        self._request_render()

    # ── Offset state ─────────────────────────────────────────────────────────

    def _load_original_offset(self) -> None:
        """Read the on-disk offset, plus the mapper's original if recorded.

        ``SongInfo`` was parsed at library-load time; the manifest may have
        been edited since (in-game, or by a previous run of this window), so
        the file is the authority for what "unchanged" means.
        """
        try:
            _, data = cinema_video.load_config(self._song.folder)
        except (OSError, ValueError):
            return
        try:
            self._original_offset_ms = int(data.get("offset", 0) or 0)
        except (TypeError, ValueError):
            self._original_offset_ms = 0
        self._offset_ms = self._original_offset_ms
        self._mapper_offset_ms = cinema_video.original_offset_ms(data)

    def _reset_target_ms(self) -> int:
        return (self._mapper_offset_ms if self._mapper_offset_ms is not None
                else self._original_offset_ms)

    def _set_offset(self, offset_ms: int) -> None:
        offset_ms = int(offset_ms)
        # Above the early return: the note describes a specific value, so even
        # a no-op set means it has been superseded by a deliberate action.
        self._detect_note = ""
        if offset_ms == self._offset_ms:
            return
        self._offset_ms = offset_ms
        self._update_offset_display()
        self._reposition_video_strips()
        self._maybe_rerender_video()
        self._push_offset_to_preview()

    def _nudge(self, delta_ms: int) -> None:
        self._set_offset(self._offset_ms + delta_ms)

    def _arrow(self, event: tk.Event, direction: int) -> str | None:
        """``direction`` is which way the waveform moves: -1 Left, +1 Right."""
        if self._typing_in_entry():
            return None  # let the arrow move the text cursor
        # Match the modifier convention used elsewhere in the app: Shift for
        # the medium step, Ctrl for the coarse one.
        if event.state & 0x0004:      # Control
            step = _STEPS[2]
        elif event.state & 0x0001:    # Shift
            step = _STEPS[1]
        else:
            step = _STEPS[0]
        self._nudge(nudge_delta_ms(step, direction))
        return "break"

    def _on_entry_commit(self, _event=None) -> None:
        raw = self._offset_var.get().strip().replace(",", "")
        try:
            value = int(round(float(raw)))
        except ValueError:
            self._update_offset_display()  # snap back to the last good value
            return
        self._set_offset(value)
        self._update_offset_display()

    def _update_offset_display(self) -> None:
        self._offset_var.set(str(self._offset_ms))
        target = self._reset_target_ms()
        label = "Reset" if self._mapper_offset_ms is None else "Reset to mapper's"
        try:
            self._reset_btn.config(
                text=label,
                state="normal" if self._offset_ms != target else "disabled",
            )
        except tk.TclError:
            pass

    def _reset(self) -> None:
        self._set_offset(self._reset_target_ms())
        self._update_offset_display()

    # ── Silence detection ────────────────────────────────────────────────────

    def _detect_silence(self) -> None:
        """Line the two files up by where the music starts in each.

        Both probes run on one worker thread — they are ffmpeg passes of a few
        hundred milliseconds each, and running them in sequence keeps the
        failure reporting simple. The result is applied rather than merely
        suggested; Reset and Cancel both still undo it, and nothing reaches
        ``cinema-video.json`` until Save.
        """
        if self._detecting:
            return
        song = self._song
        if not song.audio_path or not song.cinema_video_path:
            self._set_status("Nothing to detect — a file is missing.")
            return

        self._detecting = True
        self._detect_btn.config(text="Detecting…", state="disabled")
        self._set_status("Detecting where the music starts…")

        audio_path = Path(song.audio_path)
        video_path = Path(song.cinema_video_path)
        # Zero means "never measured", which first_sound_offset_s reads as
        # "no duration hint" rather than "zero-length file".
        song_len = self._song_duration_s or None
        video_len = self._video_duration_s or None
        ffmpeg = find_ffmpeg()

        def _work():
            try:
                song_onset = first_sound_offset_s(
                    audio_path, ffmpeg=ffmpeg, duration_s=song_len,
                )
                video_onset = first_sound_offset_s(
                    video_path, ffmpeg=ffmpeg, duration_s=video_len,
                )
            except OnsetError as exc:
                self._dispatch(lambda e=exc: self._on_detected(None, None, str(e)))
                return
            self._dispatch(
                lambda: self._on_detected(song_onset, video_onset, None)
            )

        threading.Thread(target=_work, daemon=True).start()

    def _on_detected(self, song_onset: float | None, video_onset: float | None,
                     error: str | None) -> None:
        if not self._alive():
            return
        self._detecting = False
        try:
            self._detect_btn.config(text="Detect Silence", state="normal")
        except tk.TclError:
            return

        if error is not None:
            self._set_status(f"Detection failed: {error}")
            return
        # A silent file isn't an ffmpeg failure, so it arrives as a None onset
        # and needs naming — otherwise the user is told nothing at all.
        silent = [name for name, onset in
                  (("song", song_onset), ("video", video_onset)) if onset is None]
        if silent:
            self._set_status(
                f"No sound found in the {' or '.join(silent)} — "
                "the offset was left alone."
            )
            return

        offset = detected_offset_ms(song_onset, video_onset)
        self._set_offset(offset)
        self._update_offset_display()
        note = (f"Music starts at {song_onset:.2f} s in the song and "
                f"{video_onset:.2f} s in the video → {offset:+d} ms.")
        if song_onset < 0.01 and video_onset < 0.01:
            note += " Both start immediately, so check this by ear."
        # After _set_offset: it clears the note, and may queue a re-render whose
        # completion would otherwise overwrite the status line.
        self._detect_note = note
        self._set_status(note)

    # ── View / zoom ──────────────────────────────────────────────────────────

    @property
    def _span_s(self) -> float:
        span = _ZOOMS[self._zoom_index][1]
        if span is None:
            return max(self._song_duration_s, 1.0)
        return min(span, max(self._song_duration_s, 1.0))

    @property
    def _px_per_s(self) -> float:
        return _CANVAS_W / self._span_s

    def _center_view_on(self, song_time_s: float) -> None:
        self._view_start_s = clamp_view_start(
            song_time_s, self._span_s, self._song_duration_s,
        )

    def _on_zoom_changed(self, _label=None) -> None:
        label = self._zoom_var.get()
        for i, (name, _) in enumerate(_ZOOMS):
            if name == label:
                center = self._view_start_s + self._span_s / 2
                self._zoom_index = i
                self._center_view_on(center)
                self._request_render()
                return

    def _on_overview_click(self, event: tk.Event) -> None:
        if self._song_duration_s <= 0:
            return
        frac = max(0.0, min(1.0, event.x / _CANVAS_W))
        self._center_view_on(frac * self._song_duration_s)
        self._request_render()

    # ── Dragging ─────────────────────────────────────────────────────────────

    def _on_drag_start(self, event: tk.Event) -> None:
        self._drag_origin = (event.x, self._offset_ms)

    def _on_drag_move(self, event: tk.Event) -> None:
        if self._drag_origin is None:
            return
        x0, offset0 = self._drag_origin
        self._set_offset(
            drag_to_offset_ms(offset0, event.x - x0, self._px_per_s)
        )

    def _on_drag_end(self, _event: tk.Event) -> None:
        self._drag_origin = None
        self._update_offset_display()
        # Settle whatever the engine deferred while the drag was in flight.
        self._push_offset_to_preview()

    # ── Rendering ────────────────────────────────────────────────────────────

    def _request_render(self) -> None:
        """(Re)render both detail strips for the current view and offset."""
        if self._song_duration_s <= 0:
            return
        self._render_gen += 1
        gen = self._render_gen
        pps = self._px_per_s
        span = self._span_s
        view_start = self._view_start_s
        request_offset_ms = self._offset_ms
        offset_s = request_offset_ms / 1000.0

        song_win = clamp_window(view_start, span, self._song_duration_s)
        pad = span * _PAD_FRAC
        video_win = clamp_window(
            view_start + offset_s - pad, span + 2 * pad, self._video_duration_s,
        )

        song_path = self._song.audio_path
        video_path = self._song.cinema_video_path
        ffmpeg = find_ffmpeg()
        self._set_status("Rendering waveforms…")

        def _work():
            song_img = _render_one(song_path, song_win, pps, _SONG_WAVE,
                                   _STRIP_H - 8, ffmpeg)
            video_img = _render_one(video_path, video_win, pps, _VIDEO_WAVE,
                                    _STRIP_H - 8, ffmpeg)
            self._dispatch(lambda: self._apply_render(
                gen, song_img, song_win, video_img, video_win, request_offset_ms,
            ))

        threading.Thread(target=_work, daemon=True).start()

    def _apply_render(self, gen: int, song_img, song_win, video_img, video_win,
                      request_offset_ms: int) -> None:
        if not self._alive() or gen != self._render_gen:
            return  # a newer render superseded this one
        self._photo_song = self._load_photo(song_img)
        self._photo_video = self._load_photo(video_img)
        self._video_strip_window = video_win
        # The offset this window was *computed* from, not the live one: a drag
        # that continued during the render already ate into the margin, and
        # recording the current value would hide that. (Positioning is
        # unaffected — the strip is in absolute video time either way.)
        self._render_offset_ms = request_offset_ms

        song_x = (song_time_to_x(song_win[0], self._view_start_s, self._px_per_s)
                  if song_win else 0.0)
        self._draw_strip(self._song_canvas, self._photo_song, song_x,
                         "No song audio to display")
        self._reposition_video_strips()
        self._draw_ruler()
        missing = []
        if song_img is None and self._song.audio_path:
            missing.append("song")
        if video_img is None and video_win is not None:
            missing.append("video")
        self._set_status(
            f"Could not render the {' and '.join(missing)} waveform."
            if missing else self._detect_note
        )

    @staticmethod
    def _load_photo(png: Path | None):
        if png is None:
            return None
        try:
            with Image.open(png) as img:
                return ImageTk.PhotoImage(img.convert("RGBA"))
        except Exception:
            return None

    def _draw_strip(self, canvas: tk.Canvas, photo, x: float | None,
                    empty_text: str) -> None:
        canvas.delete("all")
        if photo is None or x is None:
            canvas.create_text(
                _CANVAS_W // 2, _STRIP_H // 2, text=empty_text,
                fill=SUBTEXT_COLOR, font=("Segoe UI", 9),
            )
            return
        canvas.create_image(x, _STRIP_H // 2, image=photo, anchor="w")
        canvas.create_line(0, _STRIP_H // 2, _CANVAS_W, _STRIP_H // 2, fill=_GRID)
        self._draw_playhead(canvas)

    def _draw_playhead(self, canvas: tk.Canvas) -> None:
        x = self._playhead_x()
        if x is None:
            return
        canvas.create_line(x, 0, x, _STRIP_H, fill=_PLAYHEAD, width=1)

    def _playhead_x(self) -> float | None:
        """Canvas x of the current playhead, or None when it's out of view."""
        x = song_time_to_x(self._playhead_s(), self._view_start_s, self._px_per_s)
        return x if 0 <= x <= _CANVAS_W else None

    def _playhead_s(self) -> float:
        if self._preview_running:
            return self._preview_anchor_s
        return self._view_start_s + self._span_s / 2

    def _reposition_video_strips(self) -> None:
        """Move the already-rendered video strips to match the live offset.

        The whole point of the wide render: a nudge is a canvas coordinate
        change, not another ffmpeg pass.
        """
        if not self._alive():
            return
        # The strip was rendered in *video* time; the axis is in song time, so
        # the offset is what converts one to the other.
        window = self._video_strip_window
        x = None if window is None else video_time_to_x(
            window[0], self._offset_ms, self._view_start_s, self._px_per_s,
        )
        self._draw_strip(
            self._video_canvas, self._photo_video, x,
            "Video has no audio in this range",
        )
        self._draw_overview()

    def _maybe_rerender_video(self) -> None:
        """Re-render once the offset has eaten into the rendered margin."""
        span = self._span_s
        pad = span * _PAD_FRAC
        if self._video_strip_window is None:
            # Nothing rendered — only spend an ffmpeg pass once the offset has
            # actually dragged the video back into the visible range, or every
            # nudge past the end of the clip would queue a pointless render.
            wanted = clamp_window(
                self._view_start_s + self._offset_ms / 1000.0 - pad,
                span + 2 * pad, self._video_duration_s,
            )
            if wanted is not None:
                self._request_render()
            return
        drift_s = abs(self._offset_ms - self._render_offset_ms) / 1000.0
        if drift_s > pad * 0.75:
            self._request_render()

    def _draw_ruler(self) -> None:
        start = self._view_start_s
        span = self._span_s
        self._ruler_left.config(text=_fmt_time(start))
        self._ruler_right.config(text=_fmt_time(start + span))
        self._ruler_mid.config(text=f"▲ {_fmt_time(self._playhead_s())}")

    # ── Overview ─────────────────────────────────────────────────────────────

    def _request_overview_render(self) -> None:
        """Render whole-file strips once; the offset only repositions them."""
        if self._song_duration_s <= 0:
            return
        self._ov_render_gen += 1
        gen = self._ov_render_gen
        pps = _CANVAS_W / self._song_duration_s
        song_path = self._song.audio_path
        video_path = self._song.cinema_video_path
        video_len = self._video_duration_s
        song_len = self._song_duration_s
        ffmpeg = find_ffmpeg()
        half = _OVERVIEW_H // 2 - 2

        def _work():
            song_img = video_img = None
            try:
                if song_path:
                    song_img = waveform.render(
                        Path(song_path), duration_s=song_len,
                        width=_CANVAS_W, height=half, color=_SONG_WAVE,
                        ffmpeg=ffmpeg,
                    )
            except waveform.WaveformError:
                pass
            try:
                if video_path and video_len > 0:
                    video_img = waveform.render(
                        Path(video_path), duration_s=video_len,
                        width=max(1, int(round(video_len * pps))), height=half,
                        color=_VIDEO_WAVE, ffmpeg=ffmpeg,
                    )
            except waveform.WaveformError:
                pass
            self._dispatch(lambda: self._apply_overview(gen, song_img, video_img, pps))

        threading.Thread(target=_work, daemon=True).start()

    def _apply_overview(self, gen: int, song_img, video_img, pps: float) -> None:
        if not self._alive() or gen != self._ov_render_gen:
            return
        self._photo_ov_song = self._load_photo(song_img)
        self._photo_ov_video = self._load_photo(video_img)
        self._ov_video_px_per_s = pps
        self._draw_overview()

    def _draw_overview(self) -> None:
        c = self._ov_canvas
        if not self._alive():
            return
        c.delete("all")
        quarter = _OVERVIEW_H // 4
        if self._photo_ov_song is not None:
            c.create_image(0, quarter, image=self._photo_ov_song, anchor="w")
        if self._photo_ov_video is not None:
            # Whole-video render, so its left edge is video time 0. The offset
            # only slides it — the overview never needs a re-render.
            x = video_time_to_x(0.0, self._offset_ms, 0.0, self._ov_video_px_per_s)
            c.create_image(x, quarter * 3, image=self._photo_ov_video, anchor="w")
        c.create_line(0, _OVERVIEW_H // 2, _CANVAS_W, _OVERVIEW_H // 2, fill=_GRID)

        if self._song_duration_s > 0:
            pps = _CANVAS_W / self._song_duration_s
            x0 = self._view_start_s * pps
            x1 = (self._view_start_s + self._span_s) * pps
            c.create_rectangle(
                x0, 0, max(x1, x0 + 2), _OVERVIEW_H - 1,
                outline=_PLAYHEAD, width=1,
            )

    # ── Preview ──────────────────────────────────────────────────────────────

    @property
    def _preview_running(self) -> bool:
        return self._preview is not None

    @property
    def _split_stereo(self) -> bool:
        try:
            return bool(self._split_var.get())
        except tk.TclError:
            return False

    def _on_split_toggled(self) -> None:
        """Apply the checkbox live — no need to restart a running preview."""
        app_config.set_split_preview(self._split_stereo)
        self._update_preview_hint()
        if self._preview is not None:
            self._preview.set_split(self._split_stereo)
            self._report_preview_status()

    def _update_preview_hint(self) -> None:
        if self._split_stereo:
            # Cinema's README gives users this exact rule, so it only holds if
            # we assign the ears the same way — video left, song right, not
            # the reverse. The engines are what guarantee that; see
            # ``cinema_preview.mix_preview`` and ``TwoInstancePreview._apply_routing``.
            text = ("Listen for the two to line up. If the left ear (video) is "
                    "behind, nudge +; if it's ahead, nudge −.")
        else:
            text = "Plays from the playhead with the offset applied live."
        try:
            self._preview_lbl.config(text=text)
        except tk.TclError:
            pass

    def _report_preview_status(self) -> None:
        """Surface whatever the engine has to say, only when it changes.

        The engines report their own trouble (a video with no audio track, a
        libmpv that won't take the split-stereo graph) rather than raising, so
        this is the only place that reads it. Written on *change* rather than
        unconditionally because this runs on every nudge, and ``_set_offset``
        also queues a waveform re-render that puts "Rendering waveforms…" on
        the same line — blanking it a moment later would make the render look
        instantaneous and the status line flicker.
        """
        engine = self._preview
        note = engine.status if engine is not None else ""
        if note == self._preview_note:
            return
        self._preview_note = note
        self._set_status(note or self._detect_note)

    def _toggle_preview(self) -> None:
        if self._preview_running or self._preview_starting:
            self._stop_preview()
        else:
            self._start_preview()

    def _start_preview(self) -> None:
        """Start the preview engine, off the Tk thread.

        Asynchronous where this used to be synchronous: the default engine
        decodes the whole song before it can play a note, which is a second or
        so on a long track and would freeze the window if it ran here. The
        button reads "Preparing…" meanwhile and a second click cancels, so a
        slow start can't strand the user.
        """
        if self._preview_starting:
            return
        if load_mpv() is None:
            dialogs.show_info(
                "Preview unavailable",
                "libmpv isn't available, so the offset can't be previewed "
                "here. The waveforms and Save still work.",
                parent=self,
            )
            return
        song = self._song
        if not song.audio_path or not song.cinema_video_path:
            return

        self._pause_browser_playback()
        start_s = self._playhead_s()
        self._preview_anchor_s = start_s

        self._preview_starting = True
        self._preview_gen += 1
        gen = self._preview_gen
        self._preview_btn.config(text="…  Preparing")
        self._set_status("Preparing the preview…")

        audio_path = song.audio_path
        video_path = song.cinema_video_path
        offset_ms = self._offset_ms
        split = self._split_stereo
        wid = self._preview_canvas.winfo_id()
        volume = app_config.get_volume()

        def _work():
            try:
                engine, note = start_preview(
                    audio_path, video_path, start_s, offset_ms,
                    split=split, wid=wid, volume=volume,
                )
            except PreviewError as exc:
                self._dispatch(lambda e=exc: self._on_preview_failed(gen, str(e)))
                return
            except Exception as exc:
                self._dispatch(lambda e=exc: self._on_preview_failed(
                    gen, f"{type(e).__name__}: {e}"))
                return
            self._dispatch(lambda: self._on_preview_started(gen, engine, note))

        threading.Thread(target=_work, daemon=True).start()

    def _on_preview_started(self, gen: int, engine, note: str) -> None:
        """Adopt a started engine, or discard it if the user gave up waiting."""
        if not self._alive() or gen != self._preview_gen:
            # Stopped (or restarted) while this was loading — the engine is
            # already playing, so it has to be torn down rather than dropped.
            engine.stop()
            return
        self._preview_starting = False
        self._preview = engine
        # The offset may have moved while the engine was loading; the value it
        # started with is stale by exactly that much.
        engine.set_offset_ms(self._offset_ms)
        try:
            self._preview_btn.config(text="■  Stop")
        except tk.TclError:
            return
        self._preview_note = engine.status
        self._set_status(note or engine.status or self._detect_note)
        # Printed once per start rather than shown: it's a developer's line, and
        # it answers "is the sync wrong because of the offset, or because the
        # engine mis-positioned something" without needing another round trip.
        diag = getattr(engine, "diagnostics", None)
        if callable(diag):
            try:
                print(f"[cinema preview] {type(engine).__name__}: {diag()}")
            except Exception:
                pass
        self._preview_tick()

    def _on_preview_failed(self, gen: int, message: str) -> None:
        if not self._alive() or gen != self._preview_gen:
            return
        self._preview_starting = False
        try:
            self._preview_btn.config(text="▶  Preview")
        except tk.TclError:
            return
        self._resume_browser_playback()
        self._set_status(f"Could not start the preview: {message}")

    def _push_offset_to_preview(self) -> None:
        """Hand a new offset to a running engine.

        ``settling`` while a drag is in progress: a drag fires on every motion
        event, and the engines defer their expensive half of the work (a seek,
        or rebuilding the shifted audio) until ``_on_drag_end`` calls this again
        with the drag over.
        """
        if self._preview is None:
            return
        self._preview.set_offset_ms(
            self._offset_ms, settling=self._drag_origin is not None,
        )
        self._report_preview_status()

    def _preview_tick(self) -> None:
        engine = self._preview
        if not self._alive() or engine is None:
            return
        engine.poll()

        pos = engine.song_position_s()
        if pos is None:
            # Still loading; check back shortly.
            self._preview_after_id = self.after(120, self._preview_tick)
            return

        self._preview_anchor_s = float(pos)
        if pos >= self._song_duration_s - 0.05 and self._song_duration_s > 0:
            self._stop_preview()
            return
        if getattr(engine, "ended", False):
            self._stop_preview()
            self._set_status("The video ended, so the preview stopped.")
            return

        # Keep the playhead on screen: recentre (and re-render) only once it
        # has run off the edge, rather than scrolling every frame.
        if not (self._view_start_s <= pos <= self._view_start_s + self._span_s):
            self._center_view_on(pos)
            self._request_render()
        else:
            self._draw_strip_playheads()
            self._draw_ruler()

        self._preview_after_id = self.after(100, self._preview_tick)

    def _draw_strip_playheads(self) -> None:
        for canvas in (self._song_canvas, self._video_canvas):
            canvas.delete("playhead")
            x = self._playhead_x()
            if x is not None:
                canvas.create_line(x, 0, x, _STRIP_H, fill=_PLAYHEAD,
                                   width=1, tags="playhead")

    def _stop_preview(self) -> None:
        if self._preview_after_id is not None:
            try:
                self.after_cancel(self._preview_after_id)
            except Exception:
                pass
            self._preview_after_id = None
        self._preview_gen += 1
        self._preview_starting = False
        self._preview_note = ""
        engine, self._preview = self._preview, None
        if engine is not None:
            engine.stop()
        try:
            self._preview_btn.config(text="▶  Preview")
            self._preview_canvas.delete("all")
        except tk.TclError:
            pass
        self._resume_browser_playback()

    def _pause_browser_playback(self) -> None:
        """Silence the app's own playback so two audio streams don't overlap."""
        mp = getattr(self._browser, "_media_player", None)
        if mp is None:
            return
        try:
            if mp.is_active and not mp.is_paused:
                mp.toggle_pause()
                self._browser_playback_paused_by_us = True
        except Exception:
            pass

    def _resume_browser_playback(self) -> None:
        if not self._browser_playback_paused_by_us:
            return
        self._browser_playback_paused_by_us = False
        mp = getattr(self._browser, "_media_player", None)
        if mp is None:
            return
        try:
            if mp.is_active and mp.is_paused:
                mp.toggle_pause()
        except Exception:
            pass

    # ── Save / close ─────────────────────────────────────────────────────────

    def _save(self) -> None:
        offset = self._offset_ms
        try:
            cinema_video.save_offset(self._song.folder, offset,
                                     backup=self._allow_backup)
        except FileNotFoundError:
            dialogs.show_error(
                "Save Failed",
                "This song no longer has a cinema-video.json.",
                parent=self,
            )
            return
        except Exception as exc:
            dialogs.show_error("Save Failed", f"Could not write the offset:\n{exc}",
                               parent=self)
            return

        # Before _on_close, or its unsaved-changes guard fires on the save we
        # just completed.
        self._original_offset_ms = offset
        self._song.cinema_video_offset_ms = offset
        notify = getattr(self._browser, "_notify_cinema_offset_changed", None)
        if callable(notify):
            try:
                notify(self._song)
            except Exception:
                pass
        self._on_close()

    def _on_close(self) -> None:
        if self._dirty() and not dialogs.ask_yes_no(
            "Discard changes?",
            f"The offset was changed to {self._offset_ms} ms but not saved.\n\n"
            "Close without saving?",
            parent=self,
        ):
            return
        self._render_gen += 1  # orphan any in-flight render callbacks
        self._ov_render_gen += 1
        self._stop_preview()
        if getattr(self._browser, "_cinema_offset_window", None) is self:
            self._browser._cinema_offset_window = None
        self.destroy()

    def _dirty(self) -> bool:
        return self._offset_ms != self._original_offset_ms

    # ── Plumbing ─────────────────────────────────────────────────────────────

    def _alive(self) -> bool:
        try:
            return bool(self.winfo_exists())
        except tk.TclError:
            return False

    def _dispatch(self, callback) -> None:
        """Hop back onto the Tk thread from a worker."""
        self._browser._dispatcher.dispatch(callback)

    def _set_status(self, text: str) -> None:
        try:
            self._status_lbl.config(text=text)
        except tk.TclError:
            pass


def open_offset_editor(browser: "SongBrowser", song: "SongInfo") -> None:
    """Open (or focus) the offset editor for ``song``.

    Refuses politely rather than opening a broken window when the video isn't
    downloaded; offers to fetch ffmpeg when it's missing, since every waveform
    here comes from it.

    Whether saving writes a ``.bak`` is decided here rather than passed in, so
    that it holds however the editor was reached — "Add Cinema Video…" opens
    it directly, but the user can just as easily close it and come back via
    Shift+right-click, and the answer shouldn't change between the two.
    """
    if not song.has_playable_cinema_video:
        dialogs.show_info(
            "No video",
            "This song's Cinema video hasn't been downloaded yet.",
            parent=browser,
        )
        return

    existing = getattr(browser, "_cinema_offset_window", None)
    if existing is not None:
        try:
            if existing.winfo_exists():
                if existing._song.folder == song.folder:
                    existing.deiconify()
                    existing.lift()
                    existing.focus_force()
                    return
                # A different song: close the old editor first, but respect a
                # "no, don't discard" — otherwise the unsaved window is
                # orphaned behind the new one.
                existing._on_close()
                if existing.winfo_exists():
                    existing.lift()
                    return
        except tk.TclError:
            pass
        browser._cinema_offset_window = None

    if find_ffmpeg() is None:
        # The editor is unusable without waveforms, so unlike the visualizer's
        # optional spectrum this gates the whole feature on the download.
        ensure = getattr(browser, "_ensure_ffmpeg", None)
        if callable(ensure):
            ensure(on_ready=lambda: open_offset_editor(browser, song))
        return

    created_here = getattr(browser, "_cinema_configs_created", ())
    browser._cinema_offset_window = CinemaOffsetWindow(
        browser, song, allow_backup=str(song.folder) not in created_here,
    )
