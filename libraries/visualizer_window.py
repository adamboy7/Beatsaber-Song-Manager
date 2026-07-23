"""
Real-time spectrum visualizer window for SongBrowser.

Streams frequency-bar frames from ``ffmpeg ... showfreqs`` at real-time
pace and blits them onto a Tk canvas in sync with playback. The ffmpeg
process is suspended/resumed so the bars track pause state, and is
restarted with ``-ss <elapsed>`` on song change or window resize.

If the current song has a downloaded Cinema mod video (``cinema-video.json``
plus the referenced video file present in the song folder), the window
instead plays that video via libmpv embedded directly into the canvas
(``--wid``), seeked to line up with the song's own audio playback (accounting
for Cinema's configured offset and duration). mpv's pause property freezes the
video clock in step with the audio's frozen elapsed time, so no reseek dance
is needed on pause/resume. Once the video's window has elapsed, playback falls
back to the frequency-bar spectrum for the remainder of the song.
"""

from __future__ import annotations

import ctypes
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import tkinter as tk
from PIL import Image, ImageEnhance, ImageTk

from libraries.audio_utils import find_ffmpeg
from libraries.constants import ACCENT_COLOR, SUBTEXT_COLOR
from libraries.media_player import _create_kill_on_close_job, assign_process_to_job
from libraries.mpv_backend import load_mpv
from libraries.window_helpers import view_song

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
_MASK_THRESHOLD_LUT = [255 if _p > 10 else 0 for _p in range(256)]

_visualizer_job = None
_visualizer_job_tried = False


def _assign_to_visualizer_job(pid: int) -> None:
    global _visualizer_job, _visualizer_job_tried
    if not _visualizer_job_tried:
        _visualizer_job_tried = True
        _visualizer_job = _create_kill_on_close_job()
    assign_process_to_job(_visualizer_job, pid)


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
        # playback session (see _tick).
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
        # Embedded-mpv video backend: Cinema videos render via an in-process
        # libmpv instance embedded straight into the canvas hwnd (--wid) —
        # GPU-accelerated, native framerate, no window hunt or reparenting.
        # Created per video playback, terminated when the stream stops or
        # falls back to the spectrum (its child window would otherwise cover
        # the canvas).
        self._mpv_video = None  # mpv.MPV | None
        self._video_active: bool = False
        # Set (from mpv's event thread) when the video hits end-of-file
        # earlier than Cinema's metadata predicted; polled by _tick.
        self._video_eof = threading.Event()
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
        self._photo_size: tuple[int, int] | None = None
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

    def _enter_fullscreen(self):
        if self._is_fullscreen:
            return
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
        # working without having to click the video first.
        self._grab_keyboard_focus()

    def _exit_fullscreen(self, _event: "tk.Event | None" = None):
        if not self._is_fullscreen:
            return
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
        """Return Tk keyboard focus to this window after entering fullscreen.

        libmpv renders into a child of our own canvas and never creates a
        top-level window, so — unlike the old embedded-ffplay backend — no
        Win32 foreground-stealing counter-dance is needed anymore.
        """
        try:
            self.lift()
            self.focus_force()
        except tk.TclError:
            pass

    # ── Periodic tick: react to playback state, blit latest frame ─────────────

    def _tick(self):
        try:
            mp = self._browser._media_player
            song = mp.playing_song
            paused = mp.is_paused
            stopped = mp.is_stopped
            song_id = id(song) if song is not None else None
            session_id = mp.session_id if song is not None else None

            # A repeat of the identical SongInfo object played back-to-back
            # (e.g. the same song twice in a queue) keeps the same id(), but
            # MediaPlayer bumps session_id every time play() actually
            # (re)launches audio. Treat that as a new playback session too,
            # so per-song state (Cinema video-ended tracking, cover art,
            # elapsed-based seeking) resets just like an actual song change
            # would. Excluded when session_id is None (that's a stop, handled
            # separately below) or when already stopped. Volume changes and
            # pause/resume are live mpv property flips that don't bump
            # session_id, so they never trigger a spurious restart here.
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
                         or self._mpv_video is not None)
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
            # error). (Cinema video runs through mpv, watched separately below.)
            proc = self._ffmpeg_proc
            if proc is not None and proc.poll() is not None and not stopped:
                self._stop_stream()

            # Video watchdog: the clip hit end-of-file earlier than Cinema's
            # metadata predicted (or the metadata was absent/zero). Fall back
            # to the frequency-bar spectrum for the rest of the song. (mpv's
            # pause fully freezes its clock, so this can't misfire mid-pause.)
            if (self._video_active and self._video_eof.is_set()
                    and not stopped and not paused):
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

    def _restart_stream_at_elapsed(self):
        song = self._current_song
        if song is None or not song.audio_path:
            return
        if find_ffmpeg() is None:
            return
        self._stop_stream()
        elapsed = self._browser._media_player.elapsed_seconds() or 0.0
        self._start_stream(song, max(0.0, elapsed))

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

    def _start_stream(self, song: "SongInfo", elapsed: float):
        """Start a stream for ``song`` at ``elapsed``.

        Synchronous either way: Cinema videos start via an embedded libmpv
        instance, and if that isn't available (no libmpv DLL, no video file,
        load failure) this falls back to the spectrum stream immediately.
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

        if mode == "video":
            if self._start_mpv_video(song, elapsed):
                return
            # libmpv couldn't start the video — fall back to the frequency-bar
            # spectrum for this stream instead.
            self._stream_mode = "spectrum"

        self._start_spectrum_stream(ffmpeg, song, elapsed, w, h)

    def _start_spectrum_stream(self, ffmpeg: str, song: "SongInfo", elapsed: float,
                                w: int, h: int) -> None:
        cmd = self._build_spectrum_cmd(ffmpeg, song, elapsed, w, h)

        try:
            self._ffmpeg_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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
        # tracks wall-clock time, matching the mpv audio playback.
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

    # ── Embedded mpv video backend ───────────────────────────────────────────

    def _start_mpv_video(self, song: "SongInfo", elapsed: float) -> bool:
        """Play the Cinema video via libmpv embedded into the canvas.

        mpv renders in-process into a child window of the canvas hwnd
        (``--wid``) — no window hunt, no reparenting, no focus steal — and
        its pause property freezes the playback clock, so pause/resume stays
        aligned with the audio's frozen elapsed time without any reseeking.

        Synchronous: returns True if the video player is up, False if libmpv
        or the video file is unavailable or loading failed (the caller then
        falls back to the spectrum stream).
        """
        mpv_mod = load_mpv()
        if mpv_mod is None:
            return False
        video_path = song.cinema_video_path
        if not video_path:
            return False
        video_pos = max(0.0, self._video_pos(song, elapsed))

        try:
            player = mpv_mod.MPV(
                wid=str(self._canvas.winfo_id()),
                aid="no",   # the song's audio is already playing via MediaPlayer
                mute="yes",
                idle="yes",
                keep_open="yes",  # hold the last frame at EOF (no black flash)
                osd_level=0,
                # Don't let mpv react to keys/clicks — space-to-pause and the
                # fullscreen hotkeys belong to the Tk window, and mpv's own
                # seek/pause bindings would desync the video from the audio.
                input_default_bindings=False,
                input_vo_keyboard=False,
                input_cursor=False,
                cursor_autohide="no",
            )
        except Exception:
            return False

        self._video_eof.clear()
        eof_event = self._video_eof

        def _on_eof(_name, value, _ev=eof_event):
            # Runs on mpv's event thread; Event.set is thread-safe and the
            # Tk-side tick polls it. keep-open flips eof-reached at clip end.
            if value:
                _ev.set()

        try:
            player.observe_property("eof-reached", _on_eof)
        except Exception:
            pass

        try:
            player.loadfile(str(video_path), start=f"{video_pos:.3f}")
            # If playback is currently paused (stream launched while paused,
            # e.g. window opened or resized mid-pause), freeze immediately —
            # mpv still decodes and shows the seeked frame while paused.
            if self._browser._media_player.is_paused:
                player.pause = True
        except Exception:
            try:
                player.terminate()
            except Exception:
                pass
            return False

        self._mpv_video = player
        self._video_active = True
        self._suspended = False
        self._clear_canvas()
        self._set_status("")
        return True

    def _stop_mpv_video(self):
        """Tear down the embedded mpv player (and its child window, which
        would otherwise keep covering the canvas)."""
        player, self._mpv_video = self._mpv_video, None
        self._video_active = False
        self._video_eof.clear()
        if player is not None:
            try:
                player.terminate()
            except Exception:
                pass

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

    def _suspend_stream(self):
        if self._video_active:
            # mpv's pause property freezes decode and clock in-process — the
            # video holds its current frame and resumes exactly where the
            # audio's frozen elapsed time expects it. (This replaces the old
            # relaunch-seek-behind-then-NtSuspendProcess dance entirely.)
            player = self._mpv_video
            if player is not None:
                try:
                    player.pause = True
                except Exception:
                    pass
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

    def _resume_stream(self):
        if self._video_active:
            player = self._mpv_video
            if player is not None:
                try:
                    player.pause = False
                except Exception:
                    pass
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
        self._stop_mpv_video()
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
        if self._video_active:
            # mpv renders directly into its embedded child window; nothing to
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
            scaled_bars = freq_img.resize((w, scaled_h), Image.BILINEAR)
            freq_overlay = Image.new("RGB", (w, h), (0, 0, 0))
            freq_overlay.paste(scaled_bars, (0, h - scaled_h))
        else:
            freq_overlay = freq_img

        # Composite bars over background.
        if bg is not None:
            mask = freq_overlay.convert("L").point(_MASK_THRESHOLD_LUT)
            img = Image.composite(freq_overlay, bg, mask)
        else:
            img = freq_overlay

        try:
            if self._photo is not None and self._photo_size == img.size:
                self._photo.paste(img)
                return
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
            self._photo_size = img.size
        except tk.TclError:
            pass

    def _show_bright_art(self):
        bg = self._bg_image_bright
        if bg is None:
            return
        try:
            if self._photo is not None and self._photo_size == bg.size:
                self._photo.paste(bg)
                return
            photo = ImageTk.PhotoImage(bg)
            if self._image_id is None:
                self._image_id = self._canvas.create_image(0, 0, image=photo, anchor="nw")
            else:
                self._canvas.itemconfig(self._image_id, image=photo)
            self._photo = photo
            self._photo_size = bg.size
        except tk.TclError:
            pass

    def _clear_canvas(self):
        self._photo = None
        self._photo_size = None
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
        # Embedded mpv tracks its parent window's size on its own (it polls
        # the wid's client rect on Windows) — no live child-move needed.
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
        if mp.playing_song is None or mp.is_stopped:
            return
        if self._video_active:
            # mpv rescales its output to the resized parent window on its own —
            # paused or playing, no relaunch or reseek needed.
            self._stream_w, self._stream_h = self._canvas_size()
            return
        # The ffmpeg spectrum stream renders at a fixed size — relaunch it at
        # the new canvas size, seeked to the current position. Debounced, so
        # this fires once after the drag settles.
        if not mp.is_paused:
            self._restart_stream_at_elapsed()
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
        view_song(self._browser, song)

    # ── Refresh entry point (called from __init__ for the initial state) ──────

    def _refresh_song(self, initial: bool = False):
        mp = self._browser._media_player
        song = mp.playing_song
        self._current_song = song
        self._current_song_id = id(song) if song is not None else None
        self._current_session_id = mp.session_id if song is not None else None
        self._was_paused = mp.is_paused
        self._was_stopped = mp.is_stopped
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
        if initial and not mp.is_stopped:
            elapsed = mp.elapsed_seconds() or 0.0
            self._start_stream(song, elapsed)
