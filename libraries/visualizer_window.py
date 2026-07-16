"""
Real-time spectrum visualizer window for SongBrowser.

Streams frequency-bar frames from ``ffmpeg ... showfreqs`` at real-time
pace and blits them onto a Tk canvas in sync with playback. The ffmpeg
process is suspended/resumed alongside ffplay so the bars track pause
state, and is restarted with ``-ss <elapsed>`` on song change or window
resize.

If the current song has a downloaded Cinema mod video (``cinema-video.json``
plus the referenced video file present in the song folder), the window
instead plays that video in an embedded ffplay window (reparented into the
canvas), seeked to line up with the song's own audio playback (accounting for
Cinema's configured offset and duration). Once the video's window has elapsed,
playback falls back to the frequency-bar spectrum for the remainder of the song.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import tkinter as tk
from PIL import Image, ImageEnhance, ImageTk

from libraries.audio_utils import find_ffmpeg, find_ffplay
from libraries.constants import ACCENT_COLOR, SUBTEXT_COLOR
from libraries.media_player import _create_kill_on_close_job, assign_process_to_job

if TYPE_CHECKING:
    from Browser import SongBrowser
    from libraries.song_data import SongInfo


_BG = "#0d0d1a"
_BAR_COLOR_HEX = ACCENT_COLOR.lstrip("#")
_MIN_W, _MIN_H = 240, 80
_DEFAULT_W = 480
_FPS = 30
_FRAME_BYTES_PER_PX = 3  # rgb24
_ANIM_DURATION = 0.25  # seconds for pause/resume y-scale animation
_FFPLAY_TITLE = "BSM_Cinema_Video"  # window title used to spot the ffplay window

# ── Win32 constants for embedding ffplay's SDL window into the Tk canvas ──────
_GWL_STYLE = -16
_WS_CHILD = 0x40000000
_WS_POPUP = 0x80000000
_WS_CAPTION = 0x00C00000
_WS_THICKFRAME = 0x00040000
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_NOZORDER = 0x0004
_SWP_FRAMECHANGED = 0x0020
_SW_SHOW = 5
_LONG_PTR = ctypes.c_ssize_t


def _user32():
    return ctypes.WinDLL("user32", use_last_error=True)


_visualizer_job = None
_visualizer_job_tried = False


def _assign_to_visualizer_job(pid: int) -> None:
    global _visualizer_job, _visualizer_job_tried
    if not _visualizer_job_tried:
        _visualizer_job_tried = True
        _visualizer_job = _create_kill_on_close_job()
    assign_process_to_job(_visualizer_job, pid)


def _find_hwnd_for_pid_once(pid: int) -> int | None:
    """Single-pass search for the top-level, visible window owned by ``pid``
    (ffplay's SDL window).

    ffplay has no ``-wid`` option, so we launch it and locate its window by
    process id before reparenting it. Callers that need to wait for the
    window to appear should call this repeatedly via ``after()`` rather than
    looping/sleeping here — that would block the Tk event loop.
    """
    user32 = _user32()
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL

    found: list[int] = []

    def _cb(hwnd, _lparam):
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid and user32.IsWindowVisible(hwnd):
            found.append(int(hwnd))
            return False
        return True

    proc = EnumWindowsProc(_cb)
    user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.EnumWindows(proc, 0)
    return found[0] if found else None


def _embed_child(child_hwnd: int, parent_hwnd: int, w: int, h: int) -> None:
    """Reparent ``child_hwnd`` into ``parent_hwnd`` as a borderless child filling it."""
    user32 = _user32()
    get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
    get_long.argtypes = [wintypes.HWND, ctypes.c_int]
    get_long.restype = _LONG_PTR
    set_long.argtypes = [wintypes.HWND, ctypes.c_int, _LONG_PTR]
    set_long.restype = _LONG_PTR
    user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
    user32.SetParent.restype = wintypes.HWND
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.MoveWindow.argtypes = [
        wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.BOOL,
    ]
    user32.MoveWindow.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    user32.EnableWindow.restype = wintypes.BOOL

    child = wintypes.HWND(child_hwnd)
    parent = wintypes.HWND(parent_hwnd)

    style = get_long(child, _GWL_STYLE)
    style = (style & ~_WS_POPUP & ~_WS_CAPTION & ~_WS_THICKFRAME) | _WS_CHILD
    set_long(child, _GWL_STYLE, style)
    user32.SetParent(child, parent)
    user32.SetWindowPos(
        child, None, 0, 0, 0, 0,
        _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_FRAMECHANGED,
    )
    user32.MoveWindow(child, 0, 0, w, h, True)
    user32.ShowWindow(child, _SW_SHOW)
    # Disable input on the child so clicks/keys fall through to the Tk parent —
    # keeps our space-to-pause binding working and stops ffplay's own seek/pause
    # hotkeys from desyncing the video from the audio.
    user32.EnableWindow(child, False)


def _move_child(child_hwnd: int, w: int, h: int) -> None:
    user32 = _user32()
    user32.MoveWindow.argtypes = [
        wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.BOOL,
    ]
    user32.MoveWindow.restype = wintypes.BOOL
    user32.MoveWindow(wintypes.HWND(child_hwnd), 0, 0, w, h, True)


def _force_foreground(hwnd: int) -> None:
    """Make ``hwnd`` the foreground window and give it keyboard focus.

    SetForegroundWindow is normally refused for a window whose thread doesn't
    already own the foreground, so we temporarily AttachThreadInput to the
    current foreground thread (ffplay's) — the standard Win32 focus-steal dance.
    Needed because spawning ffplay grabs the OS foreground away from our Tk
    window, which otherwise leaves the fullscreen exit hotkeys dead until a click.
    """
    try:
        user32 = _user32()
        kernel32 = ctypes.WinDLL("kernel32")
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, wintypes.LPDWORD]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD

        target = wintypes.HWND(hwnd)
        fg = user32.GetForegroundWindow()
        cur = kernel32.GetCurrentThreadId()
        tgt_thread = user32.GetWindowThreadProcessId(target, None)
        fg_thread = (user32.GetWindowThreadProcessId(wintypes.HWND(fg), None)
                     if fg else 0)

        attached = [t for t in {fg_thread, tgt_thread} if t and t != cur]
        for t in attached:
            user32.AttachThreadInput(cur, t, True)
        try:
            user32.BringWindowToTop(target)
            user32.SetForegroundWindow(target)
            user32.SetFocus(target)
        finally:
            for t in attached:
                user32.AttachThreadInput(cur, t, False)
    except Exception:
        pass


def _suspend_pid(pid: int) -> bool:
    try:
        kernel32 = ctypes.WinDLL("kernel32")
        ntdll = ctypes.WinDLL("ntdll")
        handle = kernel32.OpenProcess(0x0800, False, pid)
        if not handle:
            return False
        try:
            return ntdll.NtSuspendProcess(handle) == 0
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def _resume_pid(pid: int) -> bool:
    try:
        kernel32 = ctypes.WinDLL("kernel32")
        ntdll = ctypes.WinDLL("ntdll")
        handle = kernel32.OpenProcess(0x0800, False, pid)
        if not handle:
            return False
        try:
            return ntdll.NtResumeProcess(handle) == 0
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


class VisualizerWindow(tk.Toplevel):
    def __init__(self, browser: "SongBrowser"):
        super().__init__(browser)
        self._browser = browser

        self._current_song: "SongInfo | None" = None
        # Duplicate SongInfo references in the queue (the same song played
        # back-to-back) represent the "same playing item" identity-wise, so
        # id() alone can't detect a fresh playback of it. We additionally
        # track MediaPlayer.session_id, which is incremented only by real
        # play() calls — including a same-song repeat — so a change in
        # session_id while the id is unchanged still counts as a new
        # playback session (see _tick). Internal relaunches (volume change,
        # unpause-after-volume-change) don't touch session_id, so they no
        # longer trip a spurious restart here.
        self._current_song_id: int | None = None
        self._current_session_id: int | None = None

        # Streaming ffmpeg subprocess + reader thread state.
        self._ffmpeg_proc: subprocess.Popen | None = None
        self._reader_stop = threading.Event()
        self._frame_lock = threading.Lock()
        self._stream_gen: int = 0
        self._latest_frame: tuple[int, bytes] | None = None
        self._stream_w = 0
        self._stream_h = 0
        # "spectrum" (default showfreqs bars) or "video" (Cinema mod video).
        self._stream_mode: str = "spectrum"
        # Embedded-ffplay video backend: when a Cinema video is playing we prefer
        # to hand it to ffplay (GPU-accelerated, full framerate) reparented into
        # the canvas, rather than decoding it frame-by-frame through the pipe.
        # Falls back to the pipe path if ffplay is missing or embedding fails.
        self._ffplay_proc: subprocess.Popen | None = None
        self._ffplay_hwnd: int | None = None
        self._use_ffplay_video: bool = False
        # In-flight "find ffplay's window and embed it" attempt, polled via
        # after() instead of blocking the Tk thread. _video_launch_gen is
        # bumped on every new attempt (or on stop) so a stale poll/result
        # from a superseded attempt is discarded instead of acted on.
        self._video_launch_gen: int = 0
        self._ffplay_pending_proc: subprocess.Popen | None = None
        self._ffplay_pending_callback = None
        # Set once a video stream has hit EOF/error for the current song, so
        # we don't keep retrying to decode it for the rest of the song.
        self._video_ended: bool = False

        # Playback-state mirrors so we only act on transitions.
        self._was_paused = False
        self._was_stopped = True
        self._suspended = False

        # Waveform vertical-scale animation state.
        self._y_scale: float = 1.0
        self._y_scale_target: float = 1.0
        self._y_scale_anim_from: float = 1.0
        self._y_scale_anim_start: float | None = None
        self._last_freq_img: Image.Image | None = None
        self._bg_image_bright: Image.Image | None = None

        # Tk-side state.
        self._photo: ImageTk.PhotoImage | None = None
        self._image_id: int | None = None
        self._resize_after_id: str | None = None
        self._tick_id: str | None = None
        self._last_canvas_size: tuple[int, int] = (0, 0)

        # Cover-art background state.
        self._bg_image_src: Image.Image | None = None
        self._bg_image: Image.Image | None = None
        self._bg_song_id: int | None = None
        self._enforcing_aspect: bool = False
        self._was_zoomed: bool = False
        self._pre_zoom_size: int = _DEFAULT_W

        # Fullscreen state.
        self._is_fullscreen: bool = False
        self._pre_fullscreen_geometry: str | None = None

        self.title("Visualizer")
        self.configure(bg=_BG)
        self.geometry(f"{_DEFAULT_W}x{_DEFAULT_W}")
        self.minsize(300, 300)
        try:
            _icon = tk.PhotoImage(file=Path(__file__).parent.parent / "Visualizer.png")
            self.iconphoto(False, _icon)
            self._icon = _icon
        except Exception:
            pass

        self._name_label = tk.Label(
            self, text="",
            font=("Segoe UI", 10, "bold"),
            bg=_BG, fg=ACCENT_COLOR,
            anchor="w", padx=10, pady=6,
        )
        self._name_label.pack(fill="x")

        self._status_label = tk.Label(
            self, text="",
            font=("Segoe UI", 9),
            bg=_BG, fg=SUBTEXT_COLOR,
            anchor="w", padx=10, pady=4,
        )

        self._canvas = tk.Canvas(self, bg=_BG, highlightthickness=0)
        self._canvas.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._status_label.pack(fill="x", side="bottom")

        self._canvas.bind("<Configure>", self._on_resize)
        self._canvas.bind("<Button-3>", self._on_right_click)
        self._name_label.bind("<Button-3>", self._on_right_click)
        self.bind("<Configure>", self._on_window_configure)
        self.bind("<space>", lambda _e: self._browser._media_player.toggle_pause())
        # Fullscreen toggles. F11 / Alt+Enter switch in and out; Escape only
        # exits (never enters) so it stays a safe "get me out" key.
        self.bind("<F11>", self._toggle_fullscreen)
        self.bind("<Alt-Return>", self._toggle_fullscreen)
        self.bind("<Escape>", self._exit_fullscreen)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Sync to whatever is currently playing right when the window opens.
        self._refresh_song(initial=True)
        # Tick at ~30 Hz; matches the ffmpeg output rate. Frames that arrive
        # between ticks are coalesced (we only render the latest).
        self._tick_id = self.after(33, self._tick)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def _on_close(self):
        if self._tick_id:
            try:
                self.after_cancel(self._tick_id)
            except Exception:
                pass
            self._tick_id = None
        if self._resize_after_id:
            try:
                self.after_cancel(self._resize_after_id)
            except Exception:
                pass
            self._resize_after_id = None
        self._stop_stream()
        self._browser._visualizer_window = None
        self.destroy()

    def _on_window_configure(self, event: tk.Event):
        if event.widget is not self or self._enforcing_aspect:
            return

        # While fullscreen the window intentionally isn't square — don't try to
        # snap it back to a 1:1 aspect ratio or we'd break out of fullscreen.
        if self._is_fullscreen:
            return

        state = self.wm_state()

        if state == 'zoomed':
            # Don't enforce aspect ratio while maximized — calling geometry()
            # here locks the window to a large explicit size and breaks restore.
            self._was_zoomed = True
            return

        if self._was_zoomed:
            # Just restored from maximized — snap back to pre-zoom size.
            self._was_zoomed = False
            size = self._pre_zoom_size
            self._enforcing_aspect = True
            self.geometry(f"{size}x{size}")
            self.after_idle(lambda: setattr(self, "_enforcing_aspect", False))
            return

        if event.width == event.height:
            self._pre_zoom_size = event.width
            return

        self._pre_zoom_size = event.width
        self._enforcing_aspect = True
        self.geometry(f"{event.width}x{event.width}")
        self.after_idle(lambda: setattr(self, "_enforcing_aspect", False))

    # ── Fullscreen ────────────────────────────────────────────────────────────

    def _toggle_fullscreen(self, _event: "tk.Event | None" = None):
        if self._is_fullscreen:
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()
        return "break"

    def _drop_frozen_video_child(self):
        """Remove a suspended (or about-to-be-suspended) embedded-ffplay child
        before window transitions.

        Fullscreen enter/exit makes Tk restyle, repack and resize windows, and
        some of those Win32 calls synchronously message child windows — with a
        suspended ffplay child that deadlocks the Tk thread. Kill the frozen
        child first; the debounced resize handler relaunches it frozen at the
        new size (elapsed is frozen while paused, so no sync is lost).

        The freeze may still be *in flight* rather than already applied:
        pausing a Cinema video relaunches ffplay and suspends it a fraction of
        a second later (see _relaunch_video_frozen / _deferred_video_suspend),
        so a fullscreen toggle right after a pause can arrive before
        self._suspended is set. If we only checked self._suspended here, that
        child would get frozen *inside* the just-transitioned window and
        deadlock the next synchronous message. So tear down any active or
        in-flight ffplay whenever playback is paused — not only once it's
        already suspended. Stopping it also cancels the pending deferred
        suspend (it no-ops when _ffplay_proc no longer matches).
        """
        try:
            paused = bool(self._browser._media_player._audio_paused)
        except Exception:
            paused = False
        freeze_in_flight = self._ffplay_pending_proc is not None
        if (self._use_ffplay_video or freeze_in_flight) and (self._suspended or paused):
            self._stop_ffplay()

    def _enter_fullscreen(self):
        if self._is_fullscreen:
            return
        self._drop_frozen_video_child()
        self._is_fullscreen = True
        # Remember the windowed geometry so we can restore it on exit.
        try:
            self._pre_fullscreen_geometry = self.geometry()
        except tk.TclError:
            self._pre_fullscreen_geometry = None
        # Hide the name/status labels and drop the canvas padding so the
        # visualization — the frequency bars or, for a Cinema video, the video
        # itself — fills the entire screen edge to edge.
        try:
            self._name_label.pack_forget()
            self._status_label.pack_forget()
            self._canvas.pack_configure(padx=0, pady=0)
        except tk.TclError:
            pass
        # -fullscreen drops the title bar and covers the taskbar. The canvas
        # <Configure> that follows restarts the ffmpeg stream at the new size
        # (via the debounced resize handler), so bars/video re-render full-res.
        try:
            self.attributes("-fullscreen", True)
        except tk.TclError:
            pass
        # Keep keyboard focus on the window so Escape / F11 / Alt+Enter keep
        # working without having to click the video first. Reassert shortly after
        # too, in case the debounced ffplay relaunch steals foreground.
        self._grab_keyboard_focus()
        self.after(150, self._grab_keyboard_focus)

    def _exit_fullscreen(self, _event: "tk.Event | None" = None):
        if not self._is_fullscreen:
            return
        self._drop_frozen_video_child()
        self._is_fullscreen = False
        try:
            self.attributes("-fullscreen", False)
        except tk.TclError:
            pass
        # Restore the labels around the canvas (name on top, status on bottom)
        # and the canvas padding.
        try:
            self._name_label.pack(fill="x", before=self._canvas)
            self._canvas.pack_configure(padx=8, pady=(0, 8))
            self._status_label.pack(fill="x", side="bottom")
        except tk.TclError:
            pass
        # Restore the pre-fullscreen (square) geometry.
        if self._pre_fullscreen_geometry:
            self._enforcing_aspect = True
            try:
                self.geometry(self._pre_fullscreen_geometry)
            except tk.TclError:
                pass
            self.after_idle(lambda: setattr(self, "_enforcing_aspect", False))
        return "break"

    def _grab_keyboard_focus(self):
        """Return OS foreground + Tk keyboard focus to this window.

        Used after entering fullscreen / (re)embedding ffplay so Escape, F11 and
        Alt+Enter keep working without the user having to click the video first.
        """
        try:
            self.lift()
            self.focus_force()
        except tk.TclError:
            pass
        if self._suspended and self._use_ffplay_video:
            return
        try:
            user32 = _user32()
            user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
            user32.GetAncestor.restype = ctypes.c_void_p
            root = user32.GetAncestor(wintypes.HWND(self.winfo_id()), 2)  # GA_ROOT
            if root:
                _force_foreground(root)
        except Exception:
            pass

    # ── Periodic tick: react to playback state, blit latest frame ─────────────

    def _tick(self):
        try:
            mp = self._browser._media_player
            song = mp.playing_song
            paused = bool(mp._audio_paused)
            stopped = bool(mp._stopped)
            song_id = id(song) if song is not None else None
            session_id = mp.session_id if song is not None else None

            # A repeat of the identical SongInfo object played back-to-back
            # (e.g. the same song twice in a queue) keeps the same id(), but
            # MediaPlayer bumps session_id every time play() actually
            # (re)launches audio. Treat that as a new playback session too,
            # so per-song state (Cinema video-ended tracking, cover art,
            # elapsed-based seeking) resets just like an actual song change
            # would. Excluded when session_id is None (that's a stop, handled
            # separately below) or when already stopped. Internal relaunches
            # (volume change, unpause-after-volume-change) don't bump
            # session_id, so they no longer trigger a spurious restart here.
            is_repeat_restart = (
                song_id is not None
                and song_id == self._current_song_id
                and session_id is not None
                and session_id != self._current_session_id
                and not stopped
            )

            # Song change (or a same-song restart): restart the stream.
            if song_id != self._current_song_id or is_repeat_restart:
                self._current_song_id = song_id
                self._current_session_id = session_id
                self._current_song = song
                self._on_song_changed(song)
                self._was_paused = paused
                self._was_stopped = stopped
            else:
                self._current_session_id = session_id
                # Pause / resume transitions.
                if stopped and not self._was_stopped:
                    self._stop_stream()
                    self._set_status("Stopped.")
                    self._clear_canvas()
                elif not stopped and self._was_stopped and song is not None:
                    # Resumed from stop with the same song still tracked
                    # (e.g. user hit Play after Stop). Re-launch the stream
                    # from the current elapsed offset.
                    self._restart_stream_at_elapsed()
                elif paused and not self._was_paused:
                    self._suspend_stream()
                elif not paused and self._was_paused:
                    self._resume_stream()
                elif (
                    not stopped and not paused and song is not None
                    and (self._ffmpeg_proc is not None
                         or self._ffplay_proc is not None)
                ):
                    # Same song, same play/pause state — but a Cinema video's
                    # active window (offset..offset+duration) may have just
                    # started or ended, which means the stream should switch
                    # between video and spectrum mode.
                    elapsed = mp.elapsed_seconds() or 0.0
                    if self._desired_mode(song, elapsed) != self._stream_mode:
                        self._restart_stream_at_elapsed()
                self._was_paused = paused
                self._was_stopped = stopped

            # Watchdog: the spectrum ffmpeg stream exited (song ended or decode
            # error). (Cinema video runs through ffplay, watched separately below.)
            proc = self._ffmpeg_proc
            if proc is not None and proc.poll() is not None and not stopped:
                self._stop_stream()

            # Embedded-ffplay watchdog: -autoexit makes ffplay quit when the clip
            # reaches its end. Fall back to the frequency-bar spectrum for the
            # rest of the song. (While paused the process is merely suspended, so
            # poll() is None and this doesn't misfire.)
            fproc = self._ffplay_proc
            if (self._use_ffplay_video and fproc is not None
                    and fproc.poll() is not None and not stopped and not paused):
                self._video_ended = True
                self._restart_stream_at_elapsed()

            # Advance y-scale animation.
            if self._y_scale_anim_start is not None:
                t = min(1.0, (time.time() - self._y_scale_anim_start) / _ANIM_DURATION)
                self._y_scale = self._y_scale_anim_from + (self._y_scale_target - self._y_scale_anim_from) * t
                if t >= 1.0:
                    self._y_scale = self._y_scale_target
                    self._y_scale_anim_start = None
                    if self._y_scale_target == 0.0:
                        self._show_bright_art()

            self._blit_latest_frame()
        except tk.TclError:
            self._tick_id = None  # window destroyed mid-tick
            return
        except Exception:
            pass  # transient error; keep the tick loop alive
        try:
            self._tick_id = self.after(33, self._tick)
        except tk.TclError:
            self._tick_id = None  # window destroyed mid-tick

    # ── Song change handling ─────────────────────────────────────────────────

    def _on_song_changed(self, song: "SongInfo | None"):
        self._stop_stream()
        self._clear_canvas()
        self._video_ended = False

        if song is None:
            self._name_label.config(text="")
            self._set_status("No song playing.")
            self._bg_image_src = None
            self._bg_image = None
            self._bg_song_id = None
            return

        name = song.display_name or song.song_name or "Unknown"
        author = f"  •  {song.author}" if song.author else ""
        self._name_label.config(text=f"♫  {name}{author}")

        if not song.audio_path:
            self._set_status("Song has no audio file.")
            return
        if find_ffmpeg() is None:
            self._set_status("ffmpeg not found — place ffmpeg.exe next to Browser.py.")
            return

        elapsed = self._browser._media_player.elapsed_seconds() or 0.0
        self._start_stream(song, elapsed)

    def _restart_stream_at_elapsed(self, bias: float = 0.0, on_video_result=None):
        song = self._current_song
        if song is None or not song.audio_path:
            return
        if find_ffmpeg() is None:
            return
        self._stop_stream()
        elapsed = self._browser._media_player.elapsed_seconds() or 0.0
        self._start_stream(song, max(0.0, elapsed + bias), on_video_result=on_video_result)

    # ── Cinema video mode selection ──────────────────────────────────────────

    def _video_pos(self, song: "SongInfo", elapsed: float) -> float:
        """Cinema video-timeline position for a given song-elapsed time.

        Cinema's ``offset`` (ms) shifts the video relative to the song: the
        video should be showing ``elapsed + offset/1000`` seconds into its
        own timeline.
        """
        return elapsed + (song.cinema_video_offset_ms / 1000.0)

    def _desired_mode(self, song: "SongInfo | None", elapsed: float) -> str:
        if song is None or not song.has_playable_cinema_video:
            return "spectrum"
        if self._video_ended:
            return "spectrum"
        video_pos = self._video_pos(song, elapsed)
        if video_pos < 0:
            return "spectrum"  # video hasn't started yet
        duration = song.cinema_video_duration_s
        if duration and video_pos >= duration:
            return "spectrum"  # video has finished; song is still playing
        return "video"

    # ── ffmpeg streaming ─────────────────────────────────────────────────────

    def _start_stream(self, song: "SongInfo", elapsed: float, on_video_result=None):
        """Start a stream for ``song`` at ``elapsed``.

        If a Cinema video is embedded, finding+reparenting ffplay's window
        happens asynchronously (see ``_try_start_ffplay_video``); pass
        ``on_video_result`` to be notified with True/False once that resolves.
        If this call starts (or falls back to) the spectrum stream instead —
        synchronously, within this call — ``on_video_result(False)`` (if given)
        is invoked before returning.
        """
        self._stop_stream()
        ffmpeg = find_ffmpeg()
        if ffmpeg is None:
            return

        w, h = self._canvas_size()
        if w < _MIN_W or h < _MIN_H:
            return

        self._stream_w = w
        self._stream_h = h
        self._load_cover_art(song, w, h)
        with self._frame_lock:
            self._latest_frame = None

        mode = self._desired_mode(song, elapsed)
        self._stream_mode = mode
        self._use_ffplay_video = False

        if mode == "video":
            # Cinema videos play via an embedded ffplay window (hardware-
            # accelerated, native framerate) reparented into the canvas.
            # Finding+reparenting its window resolves asynchronously.
            if self._try_start_ffplay_video(song, elapsed, w, h, on_result=on_video_result):
                return
            # ffplay couldn't even be launched — fall back to the frequency-bar
            # spectrum for this stream instead of the video.
            self._stream_mode = "spectrum"

        self._start_spectrum_stream(ffmpeg, song, elapsed, w, h)
        if on_video_result is not None:
            on_video_result(False)

    def _start_spectrum_stream(self, ffmpeg: str, song: "SongInfo", elapsed: float,
                                w: int, h: int) -> None:
        cmd = self._build_spectrum_cmd(ffmpeg, song, elapsed, w, h)

        try:
            self._ffmpeg_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
                bufsize=0,
            )
        except Exception as exc:
            self._ffmpeg_proc = None
            self._set_status(f"ffmpeg failed to start: {exc}")
            return

        _assign_to_visualizer_job(self._ffmpeg_proc.pid)

        self._set_status("")
        self._suspended = False
        self._stream_gen += 1
        stop_event = threading.Event()  # fresh per stream — see __init__ note
        self._reader_stop = stop_event
        thread = threading.Thread(
            target=self._reader_loop,
            args=(self._ffmpeg_proc, w, h, stop_event, self._stream_gen),
            daemon=True,
        )
        thread.start()

    def _build_spectrum_cmd(self, ffmpeg: str, song: "SongInfo", elapsed: float,
                             w: int, h: int) -> list[str]:
        # showfreqs renders a real-time frequency-bar frame per video frame.
        # `-re` paces input at native sample rate so the output frame rate
        # tracks wall-clock time, matching ffplay's audio playback.
        filter_str = (
            f"[0:a]showfreqs="
            f"mode=bar:"
            f"s={w}x{h}:"
            f"fscale=log:"
            f"ascale=cbrt:"
            f"cmode=combined:"
            f"win_size=2048:"
            f"win_func=hann:"
            f"colors=0x{_BAR_COLOR_HEX}|0x{_BAR_COLOR_HEX}"
        )
        cmd = [
            ffmpeg,
            "-nostdin",
            "-v", "error",
            "-re",
        ]
        if elapsed > 0.1:
            cmd += ["-ss", f"{elapsed:.2f}"]
        cmd += [
            "-i", str(song.audio_path),
            "-filter_complex", filter_str,
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "-r", str(_FPS),
            "pipe:1",
        ]
        return cmd

    # ── Embedded ffplay video backend ────────────────────────────────────────

    _FFPLAY_HWND_TIMEOUT_S = 3.0
    _FFPLAY_HWND_POLL_MS = 30

    def _try_start_ffplay_video(self, song: "SongInfo", elapsed: float,
                                 w: int, h: int, on_result=None) -> bool:
        """Launch ffplay for the Cinema video and start reparenting it into the canvas.

        Finding ffplay's SDL window can take up to ~3s (it has no ``-wid``
        option), so this only launches the process here and hands the wait
        off to a poll driven by ``after()`` instead of blocking the Tk thread.

        Returns True if ffplay was launched and a find/embed attempt is now in
        flight (the eventual outcome is reported to ``on_result``, if given,
        as True on success or False if embedding ultimately failed and the
        stream fell back to spectrum). Returns False if ffplay/the video path
        is unavailable or the process failed to launch — in that case nothing
        was started and the caller should fall back to spectrum itself.
        """
        ffplay = find_ffplay()
        if ffplay is None:
            return False
        video_path = song.cinema_video_path
        if not video_path:
            return False

        video_pos = max(0.0, self._video_pos(song, elapsed))
        # Spawn the window off-screen so the un-parented SDL window doesn't flash
        # on top of everything before we pull it into the canvas.
        cmd = [
            ffplay,
            "-hide_banner",
            "-loglevel", "error",
            "-noborder",
            "-left", "32000", "-top", "32000",
            "-x", str(w), "-y", str(h),
            "-window_title", _FFPLAY_TITLE,
            "-autoexit",
            "-an",  # the song's audio is already playing via the media player
        ]
        if video_pos > 0.1:
            cmd += ["-ss", f"{video_pos:.2f}"]
        cmd += ["-i", str(video_path)]

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            return False

        _assign_to_visualizer_job(proc.pid)

        self._video_launch_gen += 1
        gen = self._video_launch_gen
        self._ffplay_pending_proc = proc
        self._ffplay_pending_callback = on_result
        deadline = time.time() + self._FFPLAY_HWND_TIMEOUT_S
        self._poll_for_ffplay_hwnd(proc, gen, w, h, deadline)
        return True

    def _poll_for_ffplay_hwnd(self, proc: subprocess.Popen, gen: int,
                               w: int, h: int, deadline: float) -> None:
        """Non-blocking replacement for the old sleep-loop hwnd search.

        Each call does a single EnumWindows pass; if it comes up empty and
        we're still within the timeout, the next attempt is scheduled via
        ``after()`` instead of sleeping, so the Tk event loop keeps running.
        """
        if gen != self._video_launch_gen:
            return  # superseded by a newer attempt, or the stream was stopped
        hwnd = _find_hwnd_for_pid_once(proc.pid)
        if hwnd is None and time.time() < deadline:
            try:
                self.after(
                    self._FFPLAY_HWND_POLL_MS,
                    lambda: self._poll_for_ffplay_hwnd(proc, gen, w, h, deadline),
                )
            except tk.TclError:
                try:
                    proc.terminate()
                except Exception:
                    pass
            return
        self._finish_ffplay_embed(proc, gen, hwnd, w, h)

    def _finish_ffplay_embed(self, proc: subprocess.Popen, gen: int,
                              hwnd: "int | None", w: int, h: int) -> None:
        if gen != self._video_launch_gen or self._ffplay_pending_proc is not proc:
            # Superseded meanwhile (_stop_ffplay already owns cleanup for the
            # current attempt) — just make sure this orphan doesn't linger.
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
            return
        self._ffplay_pending_proc = None
        callback = self._ffplay_pending_callback
        self._ffplay_pending_callback = None

        if hwnd is None:
            try:
                proc.terminate()
            except Exception:
                pass
            self._embed_failed_fallback(callback)
            return

        try:
            parent = self._canvas.winfo_id()
            _embed_child(hwnd, parent, w, h)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
            self._embed_failed_fallback(callback)
            return

        self._ffplay_proc = proc
        self._ffplay_hwnd = hwnd
        self._use_ffplay_video = True
        self._suspended = False
        self._clear_canvas()
        self._set_status("")
        # Spawning the ffplay window steals the OS foreground — while fullscreen
        # that breaks the Escape / F11 / Alt+Enter exit keys until the user
        # clicks, and even windowed it silently kicks focus to whatever window
        # is now foreground (e.g. on every pause/resume of a Cinema video,
        # since resuming re-spawns ffplay to reseek it). Pull foreground +
        # keyboard focus back to our window (twice, to beat any late
        # activation by ffplay) regardless of fullscreen state.
        self._grab_keyboard_focus()
        self.after(120, self._grab_keyboard_focus)
        if callback is not None:
            callback(True)

    def _embed_failed_fallback(self, callback) -> None:
        """ffplay couldn't be embedded — fall back to the spectrum stream and
        report failure to whoever was waiting on the video-embed outcome."""
        self._stream_mode = "spectrum"
        self._use_ffplay_video = False
        song = self._current_song
        ffmpeg = find_ffmpeg()
        if song is not None and song.audio_path and ffmpeg is not None:
            elapsed = self._browser._media_player.elapsed_seconds() or 0.0
            w, h = self._canvas_size()
            self._start_spectrum_stream(ffmpeg, song, elapsed, w, h)
        if callback is not None:
            callback(False)

    def _resize_ffplay_child(self):
        hwnd = self._ffplay_hwnd
        if hwnd is None:
            return
        w, h = self._canvas_size()
        self._stream_w, self._stream_h = w, h
        if self._suspended:
            return
        try:
            _move_child(hwnd, w, h)
        except Exception:
            pass

    def _stop_ffplay(self):
        # Invalidate any in-flight find-hwnd/embed attempt so its poll loop
        # (and any late self.after callback) becomes a no-op, and clean up
        # the process it was launched for (not yet tracked as _ffplay_proc).
        self._video_launch_gen += 1
        pending = self._ffplay_pending_proc
        if pending is not None:
            try:
                if pending.poll() is None:
                    pending.terminate()
            except Exception:
                pass
        self._ffplay_pending_proc = None
        self._ffplay_pending_callback = None

        proc = self._ffplay_proc
        if proc is not None:
            # Resume first if suspended, so terminate() can actually reap it.
            if self._suspended:
                _resume_pid(proc.pid)
                self._suspended = False
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        try:
                            proc.wait(timeout=0.5)
                        except subprocess.TimeoutExpired:
                            pass
            except Exception:
                pass
        self._ffplay_proc = None
        self._ffplay_hwnd = None
        self._use_ffplay_video = False

    def _reader_loop(self, proc: subprocess.Popen, w: int, h: int,
                     stop_event: threading.Event, gen: int):
        """Read complete RGB frames from ffmpeg's stdout into the latest-frame slot."""
        frame_size = w * h * _FRAME_BYTES_PER_PX
        stdout = proc.stdout
        if stdout is None:
            return
        frame_buf = bytearray(frame_size)
        view = memoryview(frame_buf)
        filled = 0
        while not stop_event.is_set():
            try:
                n = stdout.readinto(view[filled:])
            except Exception:
                break
            if not n:
                break  # ffmpeg closed the pipe — end of song or error.
            filled += n
            if filled < frame_size:
                continue
            with self._frame_lock:
                if not stop_event.is_set():
                    self._latest_frame = (gen, bytes(frame_buf))
            filled = 0

    _VIDEO_FREEZE_DELAY_S = 0.35

    def _suspend_stream(self):
        if self._use_ffplay_video:
            if self._suspended:
                return
            # Relaunch+freeze resolves asynchronously (finding ffplay's window
            # isn't instant); _freeze_spectrum_stream runs as its fallback if
            # the relaunch can't be embedded (e.g. the video's window just
            # ended) — same "freeze whatever ended up playing" outcome as
            # before, just no longer decided synchronously here.
            self._relaunch_video_frozen(on_fallback=self._freeze_spectrum_stream)
            return
        self._freeze_spectrum_stream()

    def _freeze_spectrum_stream(self):
        proc = self._ffmpeg_proc
        if proc is None or proc.poll() is not None or self._suspended:
            return
        if _suspend_pid(proc.pid):
            self._suspended = True
            self._y_scale_anim_from = self._y_scale
            self._y_scale_target = 0.0
            self._y_scale_anim_start = time.time()

    def _relaunch_video_frozen(self, on_fallback=None):
        """Relaunch the embedded ffplay at the frozen elapsed position, then freeze it.

        The suspend must NOT happen synchronously right after launch: ffplay's
        window activation and our deferred _grab_keyboard_focus are still in
        flight, and AttachThreadInput/SetFocus against a suspended process
        hangs the Tk thread. Instead the video is seeked slightly *behind*
        elapsed, allowed to play for _VIDEO_FREEZE_DELAY_S, and suspended once
        settled — so the frame it freezes on still lands at ~elapsed.

        Finding+embedding ffplay's window is itself asynchronous, so the
        outcome isn't known when this call returns. On success the deferred
        suspend is scheduled here automatically; if it instead falls back to
        the spectrum stream, ``on_fallback`` (if given) is called so the
        caller can freeze that instead.
        """
        def after_launch(success: bool):
            if success:
                proc = self._ffplay_proc
                if proc is None:
                    return
                self.after(
                    int(self._VIDEO_FREEZE_DELAY_S * 1000),
                    lambda p=proc: self._deferred_video_suspend(p),
                )
            elif on_fallback is not None:
                on_fallback()

        self._restart_stream_at_elapsed(
            bias=-self._VIDEO_FREEZE_DELAY_S, on_video_result=after_launch,
        )

    def _deferred_video_suspend(self, proc: subprocess.Popen):
        """Freeze the relaunched ffplay, unless playback state changed meanwhile."""
        try:
            mp = self._browser._media_player
        except Exception:
            return
        if (
            self._use_ffplay_video
            and self._ffplay_proc is proc  # not replaced by a newer relaunch
            and proc.poll() is None
            and not self._suspended
            and mp._audio_paused  # user hasn't resumed during the delay
            and not mp._stopped
        ):
            if _suspend_pid(proc.pid):
                self._suspended = True

    def _resume_stream(self):
        if self._use_ffplay_video:
            # ffplay's video-only clock is wall-clock based: a suspended-then-
            # resumed process would jump ahead by the pause duration and drift
            # out of sync with the audio. Relaunch seeked to the current elapsed
            # position instead — elapsed already accounts for paused time.
            self._restart_stream_at_elapsed()
            return
        proc = self._ffmpeg_proc
        if proc is None or proc.poll() is not None or not self._suspended:
            return
        # When resuming, drop stale frames buffered before suspend so the bars
        # don't "fast-forward" through the pause gap before catching up.
        with self._frame_lock:
            self._latest_frame = None
        if _resume_pid(proc.pid):
            self._suspended = False
            self._y_scale_anim_from = self._y_scale
            self._y_scale_target = 1.0
            self._y_scale_anim_start = time.time()

    def _stop_stream(self):
        self._stop_ffplay()
        self._reader_stop.set()
        proc = self._ffmpeg_proc
        if proc is not None:
            # If suspended, resume first so terminate() can actually reap the
            # process (a suspended process can't service WM_CLOSE etc.).
            if self._suspended:
                _resume_pid(proc.pid)
                self._suspended = False
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        try:
                            proc.wait(timeout=0.5)
                        except subprocess.TimeoutExpired:
                            pass
            except Exception:
                pass
        self._ffmpeg_proc = None
        with self._frame_lock:
            self._latest_frame = None
        self._y_scale = 1.0
        self._y_scale_target = 1.0
        self._y_scale_anim_start = None
        self._last_freq_img = None

    # ── Drawing ──────────────────────────────────────────────────────────────

    def _blit_latest_frame(self):
        if self._stream_w <= 0 or self._stream_h <= 0:
            return
        if self._use_ffplay_video:
            # ffplay renders directly into its embedded child window; nothing to
            # paint on the Tk canvas.
            return
        with self._frame_lock:
            entry = self._latest_frame
            self._latest_frame = None

        data: bytes | None = None
        if entry is not None:
            gen, frame_bytes = entry
            if gen == self._stream_gen:
                data = frame_bytes

        animating = self._y_scale_anim_start is not None

        if data is None:
            if not animating:
                return
            freq_img = self._last_freq_img
            if freq_img is None:
                return
        else:
            try:
                freq_img = Image.frombytes("RGB", (self._stream_w, self._stream_h), data)
                self._last_freq_img = freq_img
            except Exception:
                return

        w, h = freq_img.size

        # Background art: crossfade dim → bright as scale goes 1 → 0.
        art_alpha = 1.0 - self._y_scale
        dim = self._bg_image
        bright = self._bg_image_bright
        if dim is not None and bright is not None and dim.size == (w, h):
            if art_alpha < 0.001:
                bg = dim
            elif art_alpha > 0.999:
                bg = bright
            else:
                bg = Image.blend(dim, bright, art_alpha)
        elif dim is not None and dim.size == (w, h):
            bg = dim
        else:
            bg = None

        # Scale bars only, anchored at the bottom — art stays full size.
        scale = self._y_scale
        if abs(scale - 1.0) > 0.001:
            scaled_h = max(1, int(h * scale))
            scaled_bars = freq_img.resize((w, scaled_h), Image.LANCZOS)
            freq_overlay = Image.new("RGB", (w, h), (0, 0, 0))
            freq_overlay.paste(scaled_bars, (0, h - scaled_h))
        else:
            freq_overlay = freq_img

        # Composite bars over background.
        if bg is not None:
            mask = freq_overlay.convert("L").point(lambda p: 255 if p > 10 else 0)
            img = Image.composite(freq_overlay, bg, mask)
        else:
            img = freq_overlay

        try:
            photo = ImageTk.PhotoImage(img)
        except Exception:
            return
        try:
            if self._image_id is None:
                self._image_id = self._canvas.create_image(
                    0, 0, image=photo, anchor="nw",
                )
            else:
                self._canvas.itemconfig(self._image_id, image=photo)
            # Keep a reference so Python doesn't GC the PhotoImage while Tk
            # still has the handle.
            self._photo = photo
        except tk.TclError:
            pass

    def _show_bright_art(self):
        bg = self._bg_image_bright
        if bg is None:
            return
        try:
            photo = ImageTk.PhotoImage(bg)
            if self._image_id is None:
                self._image_id = self._canvas.create_image(0, 0, image=photo, anchor="nw")
            else:
                self._canvas.itemconfig(self._image_id, image=photo)
            self._photo = photo
        except tk.TclError:
            pass

    def _clear_canvas(self):
        self._photo = None
        try:
            self._canvas.delete("all")
        except tk.TclError:
            pass
        self._image_id = None

    def _set_status(self, msg: str):
        try:
            self._status_label.config(text=msg)
        except tk.TclError:
            pass

    def _canvas_size(self) -> tuple[int, int]:
        try:
            w = max(_MIN_W, self._canvas.winfo_width())
            h = max(_MIN_H, self._canvas.winfo_height())
        except tk.TclError:
            return _DEFAULT_W, _DEFAULT_W
        return w, h

    # ── Cover art background ─────────────────────────────────────────────────

    def _load_cover_art(self, song: "SongInfo", w: int, h: int):
        if song is None or not song.cover_path:
            self._bg_image_src = None
            self._bg_image = None
            self._bg_image_bright = None
            return
        if self._bg_song_id != id(song):
            try:
                self._bg_image_src = Image.open(song.cover_path).convert("RGB")
            except Exception:
                self._bg_image_src = None
            self._bg_song_id = id(song)
        self._resize_cover_art(w, h)

    def _resize_cover_art(self, w: int, h: int):
        src = self._bg_image_src
        if src is None:
            self._bg_image = None
            self._bg_image_bright = None
            return
        src_w, src_h = src.size
        scale = min(w / src_w, h / src_h)
        fit_w, fit_h = int(src_w * scale), int(src_h * scale)
        scaled = src.resize((fit_w, fit_h), Image.LANCZOS)
        canvas = Image.new("RGB", (w, h), (0, 0, 0))
        canvas.paste(scaled, ((w - fit_w) // 2, (h - fit_h) // 2))
        self._bg_image_bright = canvas.copy()
        self._bg_image = ImageEnhance.Brightness(canvas).enhance(0.45)

    # ── Resize handling ──────────────────────────────────────────────────────

    def _on_resize(self, event: tk.Event):
        size = (event.width, event.height)
        if size == self._last_canvas_size:
            return
        self._last_canvas_size = size
        # For embedded ffplay, resize the child window live so it tracks the
        # canvas during the drag (ffplay adapts its SDL surface to the new size);
        # no ffmpeg restart needed.
        if self._use_ffplay_video:
            self._resize_ffplay_child()
        if self._resize_after_id:
            try:
                self.after_cancel(self._resize_after_id)
            except Exception:
                pass
        # Debounce — only restart the ffmpeg stream after the user has
        # stopped dragging the border for ~200ms.
        self._resize_after_id = self.after(200, self._after_resize_settled)

    def _after_resize_settled(self):
        self._resize_after_id = None
        # If a stream is active, restart it at the new size, resuming from
        # the current elapsed offset.
        if self._current_song is None:
            return
        mp = self._browser._media_player
        if mp.playing_song is None or mp._stopped:
            return
        # ffplay doesn't rescale its SDL surface when its window is merely
        # moved, so a live MoveWindow leaves the video at its original size.
        # Relaunch the stream at the new canvas size (seeked to the current
        # position) so the picture actually fills the resized/fullscreen
        # window. Debounced, so this fires once after the drag settles.
        if not mp._audio_paused:
            self._restart_stream_at_elapsed()
            return
        elapsed = mp.elapsed_seconds() or 0.0
        if self._desired_mode(self._current_song, elapsed) == "video":
            # Deferred freeze — suspending synchronously right after launch
            # deadlocks against the pending focus grab. Finding/embedding
            # ffplay's window is itself async now, so the "fell back to
            # spectrum, freeze that instead" case is handled via on_fallback
            # rather than checking the result synchronously here.
            self._relaunch_video_frozen(on_fallback=self._freeze_after_resize_spectrum)
            return
        self._freeze_after_resize_spectrum()

    def _freeze_after_resize_spectrum(self):
        self._restart_stream_at_elapsed()
        proc = self._ffmpeg_proc
        if proc is not None and proc.poll() is None:
            _suspend_pid(proc.pid)
            self._suspended = True
        self._y_scale = 0.0
        self._y_scale_target = 0.0
        self._y_scale_anim_start = None
        self._show_bright_art()

    # ── Context menu ─────────────────────────────────────────────────────────

    def _on_right_click(self, event: tk.Event):
        song = self._current_song
        if song is None:
            return
        menu = tk.Menu(
            self, tearoff=0,
            bg="#1e1e1e", fg="white",
            activebackground=ACCENT_COLOR, activeforeground="white", bd=0,
        )
        menu.add_command(label="View Song", command=lambda: self._view_song(song))
        menu.add_command(label="Save Image…", command=lambda: self._save_cover_art(song))
        menu.tk_popup(event.x_root, event.y_root)

    def _save_cover_art(self, song: "SongInfo"):
        import re
        import tkinter.filedialog as fd
        from tkinter import messagebox
        if not song.cover_path:
            messagebox.showinfo("No image", "This song has no cover art.", parent=self)
            return
        raw_name = song.display_name or song.song_name or "cover"
        safe_name = re.sub(r'[\\/:*?"<>|]', "", raw_name).strip() or "cover"
        path = fd.asksaveasfilename(
            title="Save Image As",
            initialfile=safe_name,
            defaultextension=".jpg",
            filetypes=[
                ("JPEG", "*.jpg *.jpeg"),
                ("PNG", "*.png"),
                ("All files", "*.*"),
            ],
            parent=self,
        )
        if not path:
            return
        try:
            img = Image.open(song.cover_path).convert("RGB")
            fmt = "PNG" if Path(path).suffix.lower() == ".png" else "JPEG"
            img.save(path, format=fmt)
        except Exception as e:
            messagebox.showerror("Error", f"Could not save image:\n{e}", parent=self)

    def _view_song(self, song: "SongInfo"):
        b = self._browser
        folder = str(song.folder)

        def _find(lst):
            for i, s in enumerate(lst):
                if s is song or str(s.folder) == folder:
                    return i
            return None

        idx = _find(b.filtered)
        if idx is None:
            b.search_var.set("")
            idx = _find(b.filtered)
            if idx is None:
                return
        b.page = idx // b.page_size
        b.selected_indices = {idx}
        b.selected_index = idx
        b._selected_folders = {str(song.folder)}
        b._render_list()
        b._scroll_to_selected()
        b.status_bar.config(text=f"Selected: {song.display_name}")
        b.lift()
        b.focus_force()

    # ── Refresh entry point (called from __init__ for the initial state) ──────

    def _refresh_song(self, initial: bool = False):
        mp = self._browser._media_player
        song = mp.playing_song
        self._current_song = song
        self._current_song_id = id(song) if song is not None else None
        self._current_session_id = mp.session_id if song is not None else None
        self._was_paused = bool(mp._audio_paused)
        self._was_stopped = bool(mp._stopped)
        if song is None:
            self._name_label.config(text="")
            self._set_status("No song playing.")
            return
        name = song.display_name or song.song_name or "Unknown"
        author = f"  •  {song.author}" if song.author else ""
        self._name_label.config(text=f"♫  {name}{author}")
        if not song.audio_path:
            self._set_status("Song has no audio file.")
            return
        if find_ffmpeg() is None:
            self._set_status("ffmpeg not found — place ffmpeg.exe next to Browser.py.")
            return
        if initial and not mp._stopped:
            elapsed = mp.elapsed_seconds() or 0.0
            self._start_stream(song, elapsed)
