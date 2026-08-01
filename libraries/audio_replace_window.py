"""Audio replacement editor.

Shows the song's current audio (fixed) above a candidate replacement
(draggable) on a shared song-time axis, and writes the aligned result over the
song's audio file.

Why this exists
---------------
The old Replace Audio was a file picker and an overwrite. That is right for the
case it was built for — a bad rip swapped for a good one, byte-aligned by
construction — and wrong for the case people actually use it for: swapping in a
*different recording of the same song*. Two covers, or a studio take and a live
one, agree on the music and disagree on everything around it: a count-in, a
half-second of room tone, a fade that runs eight bars longer. Overwriting one
with the other leaves every note in the chart pointing at the wrong moment, and
the only feedback is playing the map and discovering it is unplayable.

So the swap became an alignment: line the replacement up against the waveform
it is replacing, hear it, then write.

Timeline model
--------------
Everything on screen is drawn against **song time**, meaning the timeline the
beatmap's notes are written against. The offset maps the replacement onto it::

    song_time = new_audio_time + offset_ms / 1000

A positive offset therefore means the replacement *starts later* — silence is
padded in front of it — and a negative one trims its head. That is the plain
reading of the number, and it is deliberately the opposite sense to
``cinema_offset_window``'s. There the sign is not ours to choose: Cinema's own
menu labels ``+20 ms`` "Starts video earlier", so its editor has to invert the
drag to stay compatible with advice users already have. Nothing external
defines this offset, so it points the way the strip moves.

Length
------
The written file is padded with silence rather than left short. A chart whose
audio ends early is not merely quiet at the end — the game has notes scheduled
past the end of the stream. Trimming a *long* replacement is the arguable half,
so it is the checkbox: on by default (the result is exactly as long as what it
replaced, which is always safe), off for the case where an extended mix is
wanted and the extra minute past the last note is harmless. Even switched off,
a replacement that would come out shorter than the original is still padded —
see :func:`plan_replacement`.

Rendering and preview
---------------------
Waveforms are ffmpeg ``showwavespic`` PNGs (see ``waveform``), rendered off the
Tk thread and cached on disk, with the replacement rendered wider than the
visible span so a nudge repositions a bitmap instead of queueing another ffmpeg
pass — the same scheme, and the same ``_PAD_FRAC``, as the Cinema editor.
Preview is delegated to ``audio_ab_preview``.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import NamedTuple, TYPE_CHECKING

import tkinter as tk
from PIL import Image, ImageTk

from libraries import app_config, asset_editor, dialogs, timeline, waveform
from libraries.audio_ab_preview import (
    MODE_BOTH,
    MODE_NEW,
    MODE_ORIGINAL,
    AbPreview,
)
from libraries.audio_utils import (
    OnsetError,
    find_ffmpeg,
    first_sound_offset_s,
    get_audio_duration,
)
from libraries.cinema_preview import PreviewError
from libraries.constants import ACCENT_COLOR, SUBTEXT_COLOR, TEXT_COLOR

if TYPE_CHECKING:
    from Browser import SongBrowser
    from libraries.song_data import SongInfo

# ── Palette ──────────────────────────────────────────────────────────────────
_BG = "#0d0d1a"
_STRIP_BG = "#141422"
_GRID = "#2a2a3a"
_ORIG_WAVE = "#4ec9ff"      # cyan: the fixed reference
_NEW_WAVE = ACCENT_COLOR    # magenta: the thing you move
_PLAYHEAD = "#ffd700"
_WARN = "#ffb347"

# ── Geometry ─────────────────────────────────────────────────────────────────
_CANVAS_W = 900
_OVERVIEW_H = 54
_STRIP_H = 104

# Fraction of the visible span rendered beyond each edge of the moving strip,
# giving the user room to drag before a re-render is needed.
_PAD_FRAC = 0.6

# Nudge steps. The same three as the Cinema editor, for the same reason a piano
# has the same keys in every octave: this app now has two alignment editors and
# they should not need separate muscle memory.
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

_MODE_LABELS = (
    ("Both (split stereo)", MODE_BOTH),
    ("Original only", MODE_ORIGINAL),
    ("Replacement only", MODE_NEW),
)

_fmt_time = timeline.fmt_time


# ── Timeline math ────────────────────────────────────────────────────────────
# Free functions rather than methods: this is the part worth testing, and the
# test suite never opens a window.

clamp_window = timeline.clamp_window
clamp_view_start = timeline.clamp_view_start
song_time_to_x = timeline.song_time_to_x


def new_time_to_x(new_time_s: float, offset_ms: int,
                  view_start_s: float, px_per_s: float) -> float:
    """Canvas x of a moment in the *replacement's* timeline, on the song axis.

    Straight substitution into ``song_time = new_time + offset``: raising the
    offset slides the replacement right, which is the direction it was dragged.
    """
    return song_time_to_x(new_time_s + offset_ms / 1000.0, view_start_s, px_per_s)


def drag_to_offset_ms(base_offset_ms: int, dx_px: float, px_per_s: float) -> int:
    """Offset after dragging the replacement ``dx_px`` from ``base_offset_ms``.

    No sign flip, unlike the Cinema editor's function of the same name: the
    strip goes where the pointer goes, and the number follows it.
    """
    if px_per_s <= 0:
        return int(base_offset_ms)
    return int(round(base_offset_ms + timeline.drag_delta_ms(dx_px, px_per_s)))


def nudge_delta_ms(step_ms: int, direction: int) -> int:
    """Offset change for an arrow-key nudge.

    ``direction`` is the direction the waveform should move on screen: -1 for
    Left, +1 for Right — which here is also the direction the offset moves, so
    unlike Cinema's the arrows and the ``+``/``−`` buttons agree.
    """
    return direction * step_ms


def detected_offset_ms(original_onset_s: float, new_onset_s: float) -> int:
    """Offset that lines the two first-sounds up, in ms.

    Detection assumes the same music starts in both files and asks what offset
    makes those two moments the same instant. Substituting
    ``new_time = new_onset`` into ``song_time = new_time + offset`` and
    requiring ``song_time = original_onset`` gives the difference directly.

    So a replacement with a long silent lead-in of its own (``new_onset``
    large) gets a *negative* offset — its head is trimmed back to where the
    music actually starts, rather than pushing the music a second late.
    """
    return int(round((original_onset_s - new_onset_s) * 1000.0))


class ReplacementPlan(NamedTuple):
    """What writing the current alignment would actually do.

    Every field is in seconds of song time, and every one of them is something
    the user can be shown before committing — the point being that "replace"
    stops being a leap of faith.
    """

    target_duration_s: float | None  # what to force the output length to
    result_duration_s: float         # what the file will actually come out at
    lead_in_s: float                 # silence padded before the replacement
    head_trim_s: float               # cut from the start of the replacement
    tail_pad_s: float                # silence padded after it
    tail_trim_s: float               # cut from its end


def plan_replacement(original_s: float, new_s: float, offset_ms: int,
                     match_length: bool) -> ReplacementPlan:
    """Work out the write that the current alignment implies.

    ``match_length`` off does not mean "write whatever falls out". A result
    *longer* than the original is the case the checkbox exists for — an
    extended mix whose tail runs past the last note costs nothing. A result
    *shorter* than the original is a different thing entirely: the chart still
    has notes scheduled after the audio stops, and no setting should be able to
    produce that by accident. So the tail is padded either way, and the
    checkbox governs only whether a long replacement gets cut back.
    """
    original_s = max(0.0, float(original_s))
    new_s = max(0.0, float(new_s))
    offset_s = int(offset_ms) / 1000.0

    lead_in_s = max(0.0, offset_s)
    head_trim_s = min(new_s, max(0.0, -offset_s))
    # Where the replacement's last sample lands on the song's axis. Floored at
    # the lead-in so a head trim deeper than the file itself reads as "empty"
    # rather than as a negative length.
    natural_end_s = max(lead_in_s, offset_s + new_s)

    if match_length or natural_end_s < original_s:
        target = original_s if original_s > 0 else None
    else:
        target = None

    result_s = target if target else natural_end_s
    return ReplacementPlan(
        target_duration_s=target,
        result_duration_s=result_s,
        lead_in_s=lead_in_s,
        head_trim_s=head_trim_s,
        tail_pad_s=max(0.0, result_s - natural_end_s),
        tail_trim_s=max(0.0, natural_end_s - result_s),
    )


def describe_plan(plan: ReplacementPlan, original_s: float) -> str:
    """One line saying what the write will do, in the user's terms."""
    if original_s <= 0:
        return ""
    bits: list[str] = [f"Result: {_fmt_time(plan.result_duration_s)}"]
    if abs(plan.result_duration_s - original_s) > 0.05:
        bits[0] += f" (was {_fmt_time(original_s)})"
    for amount, phrase in (
        (plan.lead_in_s, "{} of silence in front"),
        (plan.head_trim_s, "{} trimmed from the start"),
        (plan.tail_pad_s, "{} of silence at the end"),
        (plan.tail_trim_s, "{} trimmed from the end"),
    ):
        if amount > 0.05:
            bits.append(phrase.format(f"{amount:.2f} s"))
    return " · ".join(bits)


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


class AudioReplaceWindow(tk.Toplevel):
    """Align a candidate audio file against a song's own, then write it."""

    def __init__(self, browser: "SongBrowser", song: "SongInfo", new_path: Path,
                 media_player=None, on_replaced=None):
        super().__init__(browser)
        self._browser = browser
        self._song = song
        self._new_path = Path(new_path)
        self._media_player = media_player
        self._on_replaced = on_replaced

        self._offset_ms = 0
        self._original_duration_s = 0.0
        self._new_duration_s = 0.0

        self._zoom_index = _DEFAULT_ZOOM
        self._view_start_s = 0.0

        # Rendered strips: PhotoImage refs must outlive the canvas item.
        self._photo_orig: ImageTk.PhotoImage | None = None
        self._photo_new: ImageTk.PhotoImage | None = None
        self._photo_ov_orig: ImageTk.PhotoImage | None = None
        self._photo_ov_new: ImageTk.PhotoImage | None = None
        # New strip provenance: (new_time_start, new_time_length).
        self._new_strip_window: tuple[float, float] | None = None
        self._ov_new_px_per_s = 0.0
        self._render_gen = 0
        self._ov_render_gen = 0
        # Offset the strip was rendered at, so we know how far it has been
        # dragged since and when the rendered margin runs out.
        self._render_offset_ms = 0

        self._drag_origin: tuple[int, int] | None = None

        self._detecting = False
        self._detect_note = ""

        self._preview: AbPreview | None = None
        self._preview_starting = False
        self._preview_gen = 0
        self._preview_after_id: str | None = None
        self._preview_anchor_s = 0.0
        self._browser_playback_paused_by_us = False

        self._writing = False

        self.title(f"Replace Audio — {song.display_name}")
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

        self._update_offset_display()
        self._probe_durations_async()

    # ── Construction ─────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        pad = dict(padx=14)

        header = tk.Frame(self, bg=_BG)
        header.pack(fill="x", pady=(12, 2), **pad)
        tk.Label(
            header, text=self._song.display_name, bg=_BG, fg=TEXT_COLOR,
            font=("Segoe UI", 11, "bold"), anchor="w",
        ).pack(side="left")
        self._status_lbl = tk.Label(
            header, text="Reading both files…", bg=_BG, fg=SUBTEXT_COLOR,
            font=("Segoe UI", 9), anchor="e",
        )
        self._status_lbl.pack(side="right")

        tk.Label(
            self, text=f"Replacing with:  {self._new_path.name}", bg=_BG,
            fg=_NEW_WAVE, font=("Segoe UI", 9), anchor="w",
        ).pack(fill="x", pady=(0, 8), **pad)

        # ── Overview ────────────────────────────────────────────────────────
        self._ov_canvas = tk.Canvas(
            self, width=_CANVAS_W, height=_OVERVIEW_H, bg=_STRIP_BG,
            highlightthickness=0, bd=0, cursor="hand2",
        )
        self._ov_canvas.pack(pady=(0, 8), **pad)
        self._ov_canvas.bind("<Button-1>", self._on_overview_click)
        self._ov_canvas.bind("<B1-Motion>", self._on_overview_click)

        # ── Detail strips ───────────────────────────────────────────────────
        self._orig_canvas = self._make_strip("Current audio", _ORIG_WAVE)
        self._new_canvas = self._make_strip("Replacement", _NEW_WAVE, draggable=True)

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
            text="Drag the replacement, or nudge it with ← / → "
                 "(Shift ×100 ms, Ctrl ×1000 ms). A positive offset starts it later, "
                 "padding the gap with silence.",
            bg=_BG, fg=SUBTEXT_COLOR, font=("Segoe UI", 8), anchor="w",
        ).pack(fill="x", pady=(0, 8), **pad)

        # ── Length ──────────────────────────────────────────────────────────
        length = tk.Frame(self, bg=_BG)
        length.pack(fill="x", pady=(0, 6), **pad)
        self._match_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            length, variable=self._match_var, command=self._on_match_toggled,
            text="Match the original length (trim a longer replacement)",
            bg=_BG, fg=TEXT_COLOR, activebackground=_BG, activeforeground=TEXT_COLOR,
            selectcolor=_STRIP_BG, highlightthickness=0, bd=0, anchor="w",
            font=("Segoe UI", 9), cursor="hand2",
        ).pack(side="left")
        self._plan_lbl = tk.Label(
            length, text="", bg=_BG, fg=SUBTEXT_COLOR, font=("Segoe UI", 8),
            anchor="e",
        )
        self._plan_lbl.pack(side="right")

        # ── Preview ─────────────────────────────────────────────────────────
        preview = tk.Frame(self, bg=_BG)
        preview.pack(fill="x", pady=(0, 10), **pad)
        self._preview_btn = dialogs.themed_button(
            preview, "▶  Preview", self._toggle_preview, primary=True, width=12,
        )
        self._preview_btn.pack(side="left")

        self._mode_var = tk.StringVar(value=_MODE_LABELS[0][0])
        dialogs.themed_option_menu(
            preview, textvariable=self._mode_var,
            values=[label for label, _ in _MODE_LABELS], width=19,
            command=self._on_mode_changed,
        ).pack(side="left", padx=(10, 0))

        self._preview_lbl = tk.Label(
            preview, text="", bg=_BG, fg=SUBTEXT_COLOR, font=("Segoe UI", 8),
            anchor="w", justify="left",
        )
        self._preview_lbl.pack(side="left", padx=(12, 0), fill="x", expand=True)
        self._update_preview_hint()

        # ── Buttons ─────────────────────────────────────────────────────────
        buttons = tk.Frame(self, bg=_BG)
        buttons.pack(fill="x", pady=(0, 14), **pad)
        self._save_btn = dialogs.themed_button(
            buttons, "Replace", self._save, primary=True,
        )
        self._save_btn.pack(side="right")
        dialogs.themed_button(buttons, "Cancel", self._on_close).pack(
            side="right", padx=(0, 8))
        self._reset_btn = dialogs.themed_button(buttons, "Reset", self._reset)
        self._reset_btn.pack(side="left")
        self._detect_btn = dialogs.themed_button(
            buttons, "Detect Silence", self._detect_silence,
        )
        self._detect_btn.pack(side="left", padx=(8, 0))

    def _make_strip(self, label: str, color: str, draggable: bool = False) -> tk.Canvas:
        row = tk.Frame(self, bg=_BG)
        row.pack(fill="x", padx=14, pady=(0, 4))
        tk.Label(row, text=label, bg=_BG, fg=color, font=("Segoe UI", 8, "bold"),
                 anchor="w").pack(anchor="w")
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
        dialogs.themed_button(
            parent, f"{delta_ms:+d}", lambda d=delta_ms: self._nudge(d),
            padx=8, font=("Segoe UI", 8),
        ).pack(side="left", padx=1)

    def _bind_keys(self) -> None:
        self.bind("<Left>", lambda e: self._arrow(e, -1))
        self.bind("<Right>", lambda e: self._arrow(e, 1))
        self.bind("<space>", self._on_space)
        self.bind("<Escape>", lambda e: self._on_close())

    def _typing_in_entry(self) -> bool:
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
        """Measure both files off the UI thread; neither length is known yet."""
        audio_path = self._song.audio_path
        new_path = self._new_path

        def _work():
            original = get_audio_duration(Path(audio_path)) if audio_path else None
            new = get_audio_duration(new_path)
            self._dispatch(
                lambda: self._on_durations(original or 0.0, new or 0.0)
            )

        threading.Thread(target=_work, daemon=True).start()

    def _on_durations(self, original_s: float, new_s: float) -> None:
        if not self._alive():
            return
        self._original_duration_s = original_s
        self._new_duration_s = new_s
        if original_s <= 0:
            self._set_status("Could not measure the current audio's length.")
            return
        if new_s <= 0:
            self._set_status(
                f"Could not read {self._new_path.name} — is it really audio?"
            )
            return
        self._center_view_on(0.0)
        self._update_plan_display()
        self._request_overview_render()
        self._request_render()

    # ── Offset state ─────────────────────────────────────────────────────────

    @property
    def _axis_duration_s(self) -> float:
        """Length of the drawn timeline.

        The longer of the two files, and deliberately *not* dependent on the
        offset: an axis that grew as the replacement was dragged right would
        rescale the overview under the pointer mid-drag.
        """
        return max(self._original_duration_s, self._new_duration_s, 1.0)

    def _set_offset(self, offset_ms: int) -> None:
        offset_ms = int(offset_ms)
        # Above the early return: the note describes a specific value, so even
        # a no-op set means it has been superseded by a deliberate action.
        self._detect_note = ""
        if offset_ms == self._offset_ms:
            return
        self._offset_ms = offset_ms
        self._update_offset_display()
        self._update_plan_display()
        self._reposition_new_strips()
        self._maybe_rerender_new()
        self._push_offset_to_preview()

    def _nudge(self, delta_ms: int) -> None:
        self._set_offset(self._offset_ms + delta_ms)

    def _arrow(self, event: tk.Event, direction: int) -> str | None:
        """``direction`` is which way the waveform moves: -1 Left, +1 Right."""
        if self._typing_in_entry():
            return None  # let the arrow move the text cursor
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
        try:
            self._reset_btn.config(
                state="normal" if self._offset_ms != 0 else "disabled",
            )
        except tk.TclError:
            pass

    def _reset(self) -> None:
        self._set_offset(0)
        self._update_offset_display()

    # ── The write, described ─────────────────────────────────────────────────

    def _plan(self) -> ReplacementPlan:
        return plan_replacement(
            self._original_duration_s, self._new_duration_s,
            self._offset_ms, bool(self._match_var.get()),
        )

    def _on_match_toggled(self) -> None:
        self._update_plan_display()

    def _update_plan_display(self) -> None:
        if self._original_duration_s <= 0 or self._new_duration_s <= 0:
            return
        plan = self._plan()
        empty = plan.result_duration_s <= 0.05 or plan.head_trim_s >= self._new_duration_s
        try:
            self._plan_lbl.config(
                text=("The offset leaves no audio at all."
                      if empty else describe_plan(plan, self._original_duration_s)),
                fg=_WARN if empty else SUBTEXT_COLOR,
            )
            self._save_btn.config(state="disabled" if empty or self._writing
                                  else "normal")
        except tk.TclError:
            pass

    # ── Silence detection ────────────────────────────────────────────────────

    def _detect_silence(self) -> None:
        """Line the two files up by where the music starts in each.

        The result is applied rather than merely suggested; Reset and Cancel
        both still undo it, and nothing is written until Replace.
        """
        if self._detecting:
            return
        if not self._song.audio_path:
            self._set_status("Nothing to detect — the song has no audio.")
            return

        self._detecting = True
        self._detect_btn.config(text="Detecting…", state="disabled")
        self._set_status("Detecting where the music starts…")

        original_path = Path(self._song.audio_path)
        new_path = self._new_path
        # Zero means "never measured", which first_sound_offset_s reads as "no
        # duration hint" rather than "zero-length file".
        original_len = self._original_duration_s or None
        new_len = self._new_duration_s or None
        ffmpeg = find_ffmpeg()

        def _work():
            try:
                original_onset = first_sound_offset_s(
                    original_path, ffmpeg=ffmpeg, duration_s=original_len,
                )
                new_onset = first_sound_offset_s(
                    new_path, ffmpeg=ffmpeg, duration_s=new_len,
                )
            except OnsetError as exc:
                self._dispatch(lambda e=exc: self._on_detected(None, None, str(e)))
                return
            self._dispatch(
                lambda: self._on_detected(original_onset, new_onset, None)
            )

        threading.Thread(target=_work, daemon=True).start()

    def _on_detected(self, original_onset: float | None, new_onset: float | None,
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
                  (("current audio", original_onset), ("replacement", new_onset))
                  if onset is None]
        if silent:
            self._set_status(
                f"No sound found in the {' or '.join(silent)} — "
                "the offset was left alone."
            )
            return

        offset = detected_offset_ms(original_onset, new_onset)
        self._set_offset(offset)
        self._update_offset_display()
        note = (f"Music starts at {original_onset:.2f} s in the current audio and "
                f"{new_onset:.2f} s in the replacement → {offset:+d} ms.")
        if original_onset < 0.01 and new_onset < 0.01:
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
            return self._axis_duration_s
        return min(span, self._axis_duration_s)

    @property
    def _px_per_s(self) -> float:
        return _CANVAS_W / self._span_s

    def _center_view_on(self, song_time_s: float) -> None:
        self._view_start_s = clamp_view_start(
            song_time_s, self._span_s, self._axis_duration_s,
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
        if self._original_duration_s <= 0:
            return
        frac = max(0.0, min(1.0, event.x / _CANVAS_W))
        self._center_view_on(frac * self._axis_duration_s)
        self._request_render()

    # ── Dragging ─────────────────────────────────────────────────────────────

    def _on_drag_start(self, event: tk.Event) -> None:
        self._drag_origin = (event.x, self._offset_ms)

    def _on_drag_move(self, event: tk.Event) -> None:
        if self._drag_origin is None:
            return
        x0, offset0 = self._drag_origin
        self._set_offset(drag_to_offset_ms(offset0, event.x - x0, self._px_per_s))

    def _on_drag_end(self, _event: tk.Event) -> None:
        self._drag_origin = None
        self._update_offset_display()
        # Settle whatever the engine deferred while the drag was in flight.
        self._push_offset_to_preview()

    # ── Rendering ────────────────────────────────────────────────────────────

    def _request_render(self) -> None:
        """(Re)render both detail strips for the current view and offset."""
        if self._original_duration_s <= 0:
            return
        self._render_gen += 1
        gen = self._render_gen
        pps = self._px_per_s
        span = self._span_s
        view_start = self._view_start_s
        request_offset_ms = self._offset_ms
        offset_s = request_offset_ms / 1000.0

        orig_win = clamp_window(view_start, span, self._original_duration_s)
        pad = span * _PAD_FRAC
        # Inverting song_time = new_time + offset: the visible song range maps
        # to new_time = song_time - offset.
        new_win = clamp_window(
            view_start - offset_s - pad, span + 2 * pad, self._new_duration_s,
        )

        original_path = self._song.audio_path
        new_path = self._new_path
        ffmpeg = find_ffmpeg()
        self._set_status("Rendering waveforms…")

        def _work():
            orig_img = _render_one(original_path, orig_win, pps, _ORIG_WAVE,
                                   _STRIP_H - 8, ffmpeg)
            new_img = _render_one(new_path, new_win, pps, _NEW_WAVE,
                                  _STRIP_H - 8, ffmpeg)
            self._dispatch(lambda: self._apply_render(
                gen, orig_img, orig_win, new_img, new_win, request_offset_ms,
            ))

        threading.Thread(target=_work, daemon=True).start()

    def _apply_render(self, gen: int, orig_img, orig_win, new_img, new_win,
                      request_offset_ms: int) -> None:
        if not self._alive() or gen != self._render_gen:
            return  # a newer render superseded this one
        self._photo_orig = self._load_photo(orig_img)
        self._photo_new = self._load_photo(new_img)
        self._new_strip_window = new_win
        # The offset this window was *computed* from, not the live one: a drag
        # that continued during the render already ate into the margin, and
        # recording the current value would hide that.
        self._render_offset_ms = request_offset_ms

        orig_x = (song_time_to_x(orig_win[0], self._view_start_s, self._px_per_s)
                  if orig_win else 0.0)
        self._draw_strip(self._orig_canvas, self._photo_orig, orig_x,
                         "The current audio has ended by here")
        self._reposition_new_strips()
        self._draw_ruler()
        # Both are conditional on the window existing, not merely on the file:
        # a replacement longer than the original scrolls the view past the end
        # of the original, and an empty render there is the correct outcome
        # rather than a failure to report.
        missing = []
        if orig_img is None and orig_win is not None:
            missing.append("current audio")
        if new_img is None and new_win is not None:
            missing.append("replacement")
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
        if x is not None:
            canvas.create_line(x, 0, x, _STRIP_H, fill=_PLAYHEAD, width=1)

    def _playhead_x(self) -> float | None:
        """Canvas x of the current playhead, or None when it's out of view."""
        x = song_time_to_x(self._playhead_s(), self._view_start_s, self._px_per_s)
        return x if 0 <= x <= _CANVAS_W else None

    def _playhead_s(self) -> float:
        if self._preview_running:
            return self._preview_anchor_s
        return self._view_start_s + self._span_s / 2

    def _reposition_new_strips(self) -> None:
        """Move the already-rendered strip to match the live offset.

        The whole point of the wide render: a nudge is a canvas coordinate
        change, not another ffmpeg pass.
        """
        if not self._alive():
            return
        window = self._new_strip_window
        x = None if window is None else new_time_to_x(
            window[0], self._offset_ms, self._view_start_s, self._px_per_s,
        )
        self._draw_strip(
            self._new_canvas, self._photo_new, x,
            "The replacement has no audio in this range",
        )
        self._draw_overview()

    def _maybe_rerender_new(self) -> None:
        """Re-render once the offset has eaten into the rendered margin."""
        span = self._span_s
        pad = span * _PAD_FRAC
        if self._new_strip_window is None:
            # Nothing rendered — only spend an ffmpeg pass once the offset has
            # actually dragged the replacement back into the visible range, or
            # every nudge past its end would queue a pointless render.
            wanted = clamp_window(
                self._view_start_s - self._offset_ms / 1000.0 - pad,
                span + 2 * pad, self._new_duration_s,
            )
            if wanted is not None:
                self._request_render()
            return
        drift_s = abs(self._offset_ms - self._render_offset_ms) / 1000.0
        if drift_s > pad * 0.75:
            self._request_render()

    def _draw_ruler(self) -> None:
        start = self._view_start_s
        self._ruler_left.config(text=_fmt_time(start))
        self._ruler_right.config(text=_fmt_time(start + self._span_s))
        self._ruler_mid.config(text=f"▲ {_fmt_time(self._playhead_s())}")

    # ── Overview ─────────────────────────────────────────────────────────────

    def _request_overview_render(self) -> None:
        """Render whole-file strips once; the offset only repositions them."""
        if self._original_duration_s <= 0:
            return
        self._ov_render_gen += 1
        gen = self._ov_render_gen
        pps = _CANVAS_W / self._axis_duration_s
        original_path = self._song.audio_path
        new_path = self._new_path
        original_len = self._original_duration_s
        new_len = self._new_duration_s
        ffmpeg = find_ffmpeg()
        half = _OVERVIEW_H // 2 - 2

        def _work():
            orig_img = new_img = None
            try:
                if original_path:
                    orig_img = waveform.render(
                        Path(original_path), duration_s=original_len,
                        width=max(1, int(round(original_len * pps))), height=half,
                        color=_ORIG_WAVE, ffmpeg=ffmpeg,
                    )
            except waveform.WaveformError:
                pass
            try:
                if new_len > 0:
                    new_img = waveform.render(
                        new_path, duration_s=new_len,
                        width=max(1, int(round(new_len * pps))), height=half,
                        color=_NEW_WAVE, ffmpeg=ffmpeg,
                    )
            except waveform.WaveformError:
                pass
            self._dispatch(lambda: self._apply_overview(gen, orig_img, new_img, pps))

        threading.Thread(target=_work, daemon=True).start()

    def _apply_overview(self, gen: int, orig_img, new_img, pps: float) -> None:
        if not self._alive() or gen != self._ov_render_gen:
            return
        self._photo_ov_orig = self._load_photo(orig_img)
        self._photo_ov_new = self._load_photo(new_img)
        self._ov_new_px_per_s = pps
        self._draw_overview()

    def _draw_overview(self) -> None:
        c = self._ov_canvas
        if not self._alive():
            return
        c.delete("all")
        quarter = _OVERVIEW_H // 4
        if self._photo_ov_orig is not None:
            c.create_image(0, quarter, image=self._photo_ov_orig, anchor="w")
        if self._photo_ov_new is not None:
            # Whole-file render, so its left edge is new time 0. The offset only
            # slides it — the overview never needs a re-render.
            x = new_time_to_x(0.0, self._offset_ms, 0.0, self._ov_new_px_per_s)
            c.create_image(x, quarter * 3, image=self._photo_ov_new, anchor="w")
        c.create_line(0, _OVERVIEW_H // 2, _CANVAS_W, _OVERVIEW_H // 2, fill=_GRID)

        pps = _CANVAS_W / self._axis_duration_s
        x0 = self._view_start_s * pps
        x1 = (self._view_start_s + self._span_s) * pps
        c.create_rectangle(
            x0, 0, max(x1, x0 + 2), _OVERVIEW_H - 1, outline=_PLAYHEAD, width=1,
        )

    # ── Preview ──────────────────────────────────────────────────────────────

    @property
    def _preview_running(self) -> bool:
        return self._preview is not None and self._preview.is_running

    def _mode(self) -> str:
        label = self._mode_var.get()
        for name, mode in _MODE_LABELS:
            if name == label:
                return mode
        return MODE_BOTH

    def _update_preview_hint(self) -> None:
        hints = {
            MODE_BOTH: "Current audio in your left ear, the replacement in your "
                       "right — a flam between them means they aren't lined up.",
            MODE_ORIGINAL: "Playing the song's current audio on its own.",
            MODE_NEW: "Playing the replacement alone, where the offset puts it.",
        }
        try:
            self._preview_lbl.config(text=hints.get(self._mode(), ""))
        except tk.TclError:
            pass

    def _on_mode_changed(self, _label=None) -> None:
        self._update_preview_hint()
        if self._preview is not None:
            self._preview.set_mode(self._mode())

    def _toggle_preview(self) -> None:
        if self._preview_running or self._preview_starting:
            self._stop_preview()
        else:
            self._start_preview()

    def _start_preview(self) -> None:
        if self._original_duration_s <= 0 or self._new_duration_s <= 0:
            self._set_status("Still reading the files…")
            return
        self._preview_starting = True
        self._preview_gen += 1
        gen = self._preview_gen
        self._preview_btn.config(text="Preparing…")
        self._set_status("Decoding both files for the preview…")
        self._pause_browser_playback()

        at = self._playhead_s()
        offset = self._offset_ms
        mode = self._mode()
        # The app's own volume, not mpv's default: this preview is loud enough
        # to be startling on headphones, and the user already set a level for
        # the player it just paused.
        engine = AbPreview(Path(self._song.audio_path), self._new_path,
                           volume=app_config.get_volume())

        def _work():
            try:
                engine.start(at, offset, mode)
            except Exception as exc:
                try:
                    engine.stop()
                except Exception:
                    pass
                detail = (exc if isinstance(exc, PreviewError)
                          else f"{type(exc).__name__}: {exc}")
                self._dispatch(lambda: self._on_preview_failed(gen, str(detail)))
                return
            self._dispatch(lambda: self._on_preview_started(gen, engine))

        threading.Thread(target=_work, daemon=True).start()

    def _on_preview_started(self, gen: int, engine: AbPreview) -> None:
        if not self._alive() or gen != self._preview_gen:
            engine.stop()  # superseded while it was starting
            return
        self._preview_starting = False
        self._preview = engine
        try:
            self._preview_btn.config(text="■  Stop")
        except tk.TclError:
            engine.stop()
            return
        self._set_status(self._detect_note)
        self._preview_tick()

    def _on_preview_failed(self, gen: int, message: str) -> None:
        if not self._alive() or gen != self._preview_gen:
            return
        self._preview_starting = False
        self._preview = None
        try:
            self._preview_btn.config(text="▶  Preview")
        except tk.TclError:
            return
        self._set_status(f"Preview unavailable — {message}")
        self._resume_browser_playback()

    def _push_offset_to_preview(self) -> None:
        if self._preview is None:
            return
        self._preview.set_offset_ms(
            self._offset_ms, settling=self._drag_origin is not None,
        )

    def _preview_tick(self) -> None:
        engine = self._preview
        if not self._alive() or engine is None:
            return
        engine.poll()

        pos = engine.song_position_s()
        if pos is None:
            self._preview_after_id = self.after(120, self._preview_tick)
            return

        self._preview_anchor_s = float(pos)
        if engine.ended:
            self._stop_preview()
            return

        # Keep the playhead on screen: recentre (and re-render) only once it has
        # run off the edge, rather than scrolling every frame.
        if not (self._view_start_s <= pos <= self._view_start_s + self._span_s):
            self._center_view_on(pos)
            self._request_render()
        else:
            self._draw_strip_playheads()
            self._draw_ruler()

        self._preview_after_id = self.after(100, self._preview_tick)

    def _draw_strip_playheads(self) -> None:
        for canvas in (self._orig_canvas, self._new_canvas):
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
        engine, self._preview = self._preview, None
        if engine is not None:
            engine.stop()
        try:
            self._preview_btn.config(text="▶  Preview")
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

    # ── Write ────────────────────────────────────────────────────────────────

    def _save(self) -> None:
        """Render the aligned replacement over the song's audio.

        The handle dance is inherited from the function this window replaced,
        and is not optional on Windows: libmpv holds the audio file open while
        the song is loaded, the visualizer's ffmpeg may hold it too, and an
        in-place overwrite against either fails with a sharing violation. The
        preview is stopped first for the same reason — it is another mpv
        instance, and it is one this window started.
        """
        if self._writing or self._original_duration_s <= 0:
            return
        if self._new_duration_s <= 0:
            # The probe couldn't read the picked file. Writing would hand
            # ffmpeg the same unreadable input and fail a minute later, over a
            # song whose audio has already been released.
            self._set_status(f"{self._new_path.name} couldn't be read as audio.")
            return
        plan = self._plan()
        self._stop_preview()

        mp = self._media_player
        is_loaded = mp is not None and self._song is mp.playing_song
        was_active = is_loaded and mp.is_active
        was_paused = was_active and mp.is_paused
        if is_loaded:
            mp.stop_and_wait()
        release = getattr(self._browser, "_release_song_audio", None)
        if release is not None:
            try:
                release(self._song)
            except Exception:
                pass  # best-effort; the write may still succeed

        self._writing = True
        self._save_btn.config(text="Replacing…", state="disabled")
        self._set_status("Rendering the replacement…")

        audio_path = self._song.audio_path
        new_path = str(self._new_path)
        offset = self._offset_ms
        target = plan.target_duration_s
        ffmpeg = find_ffmpeg() or ""

        def _work():
            try:
                asset_editor.replace_audio(
                    audio_path, new_path, ffmpeg,
                    offset_ms=offset, target_duration_s=target,
                )
            except Exception as exc:
                self._dispatch(lambda e=exc: self._on_write_failed(str(e)))
                return
            self._dispatch(lambda: self._on_write_done(was_active, was_paused))

        threading.Thread(target=_work, daemon=True).start()

    def _on_write_failed(self, message: str) -> None:
        if not self._alive():
            return
        self._writing = False
        try:
            self._save_btn.config(text="Replace", state="normal")
        except tk.TclError:
            return
        dialogs.show_error("Replace Audio Failed", message, parent=self)
        self._set_status("Nothing was written — the song is untouched.")

    def _on_write_done(self, was_active: bool, was_paused: bool) -> None:
        self._writing = False
        callback = self._on_replaced
        # Close first: the callback resumes playback on the song whose audio
        # just changed, and this window should not still be sitting over it.
        self._close_now()
        if callback is not None:
            callback(was_active, was_paused)

    # ── Close ────────────────────────────────────────────────────────────────

    def _on_close(self) -> None:
        if self._writing:
            return  # a render is mid-flight; let it finish or fail
        if self._offset_ms != 0 and not dialogs.ask_yes_no(
            "Discard changes?",
            f"The replacement was aligned to {self._offset_ms:+d} ms but "
            "hasn't been written.\n\nClose without replacing?",
            parent=self,
        ):
            return
        self._close_now()

    def _close_now(self) -> None:
        self._render_gen += 1  # orphan any in-flight render callbacks
        self._ov_render_gen += 1
        self._stop_preview()
        if getattr(self._browser, "_audio_replace_window", None) is self:
            self._browser._audio_replace_window = None
        try:
            self.destroy()
        except tk.TclError:
            pass

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


def open_audio_replace_editor(browser: "SongBrowser", song: "SongInfo",
                              new_path: Path, media_player=None,
                              on_replaced=None) -> AudioReplaceWindow | None:
    """Open the alignment editor for a picked replacement file.

    One at a time: a second editor would be a second candidate for the same
    audio file, and whichever wrote last would silently win.
    """
    existing = getattr(browser, "_audio_replace_window", None)
    if existing is not None:
        try:
            if existing.winfo_exists():
                existing._on_close()
                if existing.winfo_exists():
                    existing.lift()
                    return None
        except tk.TclError:
            pass
        browser._audio_replace_window = None

    window = AudioReplaceWindow(browser, song, Path(new_path), media_player,
                                on_replaced)
    browser._audio_replace_window = window
    return window
