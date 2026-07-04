"""
Real-time spectrum visualizer window for SongBrowser.

Streams frequency-bar frames from ``ffmpeg ... showfreqs`` at real-time
pace and blits them onto a Tk canvas in sync with playback. The ffmpeg
process is suspended/resumed alongside ffplay so the bars track pause
state, and is restarted with ``-ss <elapsed>`` on song change or window
resize.
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

if TYPE_CHECKING:
    from Browser import SongBrowser
    from libraries.song_data import SongInfo


_BG = "#0d0d1a"
_BAR_COLOR_HEX = ACCENT_COLOR.lstrip("#")
_MIN_W, _MIN_H = 240, 80
_DEFAULT_W = 480
_FPS = 30
_FRAME_BYTES_PER_PX = 3  # rgb24
_PIPE_READ_CHUNK = 65536
_ANIM_DURATION = 0.25  # seconds for pause/resume y-scale animation


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
        # Identity-only — duplicate SongInfo references in the queue still
        # represent the "same playing item" so we don't restart the stream
        # when the queue loops to the same object.
        self._current_song_id: int | None = None

        # Streaming ffmpeg subprocess + reader thread state.
        self._ffmpeg_proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._frame_lock = threading.Lock()
        self._latest_frame: bytes | None = None
        self._stream_w = 0
        self._stream_h = 0

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
        self._y_scale_paused: bool = False

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

    # ── Periodic tick: react to playback state, blit latest frame ─────────────

    def _tick(self):
        try:
            mp = self._browser._media_player
            song = mp.playing_song
            paused = bool(mp._audio_paused)
            stopped = bool(mp._stopped)
            song_id = id(song) if song is not None else None

            # Song change: restart the stream.
            if song_id != self._current_song_id:
                self._current_song_id = song_id
                self._current_song = song
                self._on_song_changed(song)
                self._was_paused = paused
                self._was_stopped = stopped
            else:
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
                self._was_paused = paused
                self._was_stopped = stopped

            # Watchdog: ffmpeg exited unexpectedly (file ended, decode error,
            # etc.) — clear the canvas so we don't keep showing the last frame
            # frozen on screen.
            proc = self._ffmpeg_proc
            if proc is not None and proc.poll() is not None and not stopped:
                self._stop_stream()

            # Advance y-scale animation.
            if self._y_scale_anim_start is not None:
                t = min(1.0, (time.time() - self._y_scale_anim_start) / _ANIM_DURATION)
                self._y_scale = self._y_scale_anim_from + (self._y_scale_target - self._y_scale_anim_from) * t
                if t >= 1.0:
                    self._y_scale = self._y_scale_target
                    self._y_scale_anim_start = None
                    if self._y_scale_target == 0.0:
                        self._show_bright_art()
                        self._y_scale_paused = True

            self._blit_latest_frame()
            self._tick_id = self.after(33, self._tick)
        except tk.TclError:
            self._tick_id = None  # window destroyed mid-tick

    # ── Song change handling ─────────────────────────────────────────────────

    def _on_song_changed(self, song: "SongInfo | None"):
        self._stop_stream()
        self._clear_canvas()

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
        self._start_stream(song, elapsed)

    # ── ffmpeg streaming ─────────────────────────────────────────────────────

    def _start_stream(self, song: "SongInfo", elapsed: float):
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

        self._set_status("")
        self._suspended = False
        self._reader_stop.clear()
        thread = threading.Thread(
            target=self._reader_loop,
            args=(self._ffmpeg_proc, w, h, self._reader_stop),
            daemon=True,
        )
        self._reader_thread = thread
        thread.start()

    def _reader_loop(self, proc: subprocess.Popen, w: int, h: int,
                     stop_event: threading.Event):
        """Read complete RGB frames from ffmpeg's stdout into the latest-frame slot."""
        frame_size = w * h * _FRAME_BYTES_PER_PX
        buf = bytearray()
        stdout = proc.stdout
        if stdout is None:
            return
        while not stop_event.is_set():
            try:
                chunk = stdout.read(_PIPE_READ_CHUNK)
            except Exception:
                break
            if not chunk:
                break  # ffmpeg closed the pipe — end of song or error.
            buf.extend(chunk)
            # Drain whole frames; if we accumulated more than one frame's worth
            # (e.g. coming out of a suspend), keep only the most recent.
            if len(buf) >= frame_size:
                # Index of the most-recent complete frame's start in buf.
                whole = (len(buf) // frame_size) * frame_size
                last_frame_start = whole - frame_size
                frame = bytes(buf[last_frame_start:whole])
                # Discard everything up through the consumed frames.
                del buf[:whole]
                with self._frame_lock:
                    self._latest_frame = frame

    def _suspend_stream(self):
        proc = self._ffmpeg_proc
        if proc is None or proc.poll() is not None or self._suspended:
            return
        if _suspend_pid(proc.pid):
            self._suspended = True
            self._y_scale_anim_from = self._y_scale
            self._y_scale_target = 0.0
            self._y_scale_anim_start = time.time()
            self._y_scale_paused = False

    def _resume_stream(self):
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
            self._y_scale_paused = False

    def _stop_stream(self):
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
        self._reader_thread = None
        with self._frame_lock:
            self._latest_frame = None
        self._y_scale = 1.0
        self._y_scale_target = 1.0
        self._y_scale_anim_start = None
        self._last_freq_img = None
        self._y_scale_paused = False

    # ── Drawing ──────────────────────────────────────────────────────────────

    def _blit_latest_frame(self):
        if self._stream_w <= 0 or self._stream_h <= 0:
            return
        with self._frame_lock:
            data = self._latest_frame
            self._latest_frame = None

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
        self._restart_stream_at_elapsed()
        if mp._audio_paused:
            proc = self._ffmpeg_proc
            if proc is not None and proc.poll() is None:
                _suspend_pid(proc.pid)
                self._suspended = True
            self._y_scale = 0.0
            self._y_scale_target = 0.0
            self._y_scale_anim_start = None
            self._y_scale_paused = True
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

    # ── Refresh entry point (called from __init__ for initial state) ──────────

    def _refresh_song(self, initial: bool = False):
        mp = self._browser._media_player
        song = mp.playing_song
        self._current_song = song
        self._current_song_id = id(song) if song is not None else None
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
