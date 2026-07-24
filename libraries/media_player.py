import os
import time
import ctypes
import ctypes.wintypes
from tkinter import messagebox

from libraries.audio_utils import get_audio_duration
from libraries import mpv_installer
from libraries.mpv_backend import LIBMPV_HINT, dll_present, install_dir, load_error, load_mpv


def _mpv_unavailable_message() -> str:
    """Specific failure reason from the loader when known, generic hint otherwise."""
    reason = load_error()
    return f"{reason}" if reason else LIBMPV_HINT
from libraries.song_data import SongInfo


def _create_kill_on_close_job():
    """Create a Windows Job Object that kills all assigned processes when the handle closes.

    Audio playback is in-process (libmpv) and doesn't need this, but the
    visualizer still uses it to tie its ffmpeg spectrum subprocess to the app's
    lifetime.
    """
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = ctypes.wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = (ctypes.wintypes.LPVOID, ctypes.wintypes.LPCWSTR)
        kernel32.SetInformationJobObject.restype = ctypes.wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = (
            ctypes.wintypes.HANDLE, ctypes.c_int, ctypes.wintypes.LPVOID, ctypes.wintypes.DWORD,
        )
        kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
        kernel32.CloseHandle.argtypes = (ctypes.wintypes.HANDLE,)

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        class _BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.wintypes.DWORD),
                ("SchedulingClass", ctypes.wintypes.DWORD),
            ]

        class _IO(ctypes.Structure):
            _fields_ = [(f, ctypes.c_uint64) for f in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class _EXT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BASIC),
                ("IoInfo", _IO),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = _EXT()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def assign_process_to_job(job, pid: int) -> None:
    """Assign ``pid`` to a kill-on-close Job so it dies with this process.

    Best-effort: silently a no-op if ``job`` is None or assignment fails.
    """
    if not job:
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (
            ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD,
        )
        kernel32.AssignProcessToJobObject.restype = ctypes.wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (ctypes.wintypes.HANDLE, ctypes.wintypes.HANDLE)
        kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
        kernel32.CloseHandle.argtypes = (ctypes.wintypes.HANDLE,)

        # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        h = kernel32.OpenProcess(0x0101, False, pid)
        if h:
            kernel32.AssignProcessToJobObject(job, h)
            kernel32.CloseHandle(h)
    except Exception:
        pass


class MediaPlayer:
    """Audio playback via an in-process libmpv instance.

    One persistent mpv player is created lazily on first play and reused for
    every song. Pause, volume and position are live properties — no process
    suspension, no relaunch-and-reseek on volume change.

    A wall-clock elapsed estimate (_play_start / _paused_total) is kept as a
    fallback for the brief moments mpv's time-pos is unavailable (file still
    loading), so elapsed_seconds() never goes backwards on callers.
    """

    def __init__(self, dispatch_fn=None, status_cb=None):
        self._player = None  # lazily-created mpv.MPV | None
        self._audio_paused: bool = False
        self._stopped: bool = False
        self._looping: bool = False
        self.playing_song: SongInfo | None = None
        self._kb_listener = None

        self._dispatch_fn = dispatch_fn
        self._status_cb = status_cb

        # True once the current file reached end-of-file (mpv keep-open pauses
        # on the last sample instead of going idle, and flips eof-reached).
        # Set from mpv's event thread; plain bool store is atomic under the GIL.
        self._finished: bool = False

        self._play_start: float | None = None
        self._pause_start: float | None = None
        self._paused_total: float = 0.0
        self.song_duration: float | None = None
        self._volume: int = 75
        self.session_id: int = 0

    # ── Public state helpers ─────────────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        """A song is loaded (playing or paused) and not explicitly stopped."""
        return self.playing_song is not None and not self._stopped

    @property
    def is_paused(self) -> bool:
        """Playback is explicitly paused (not stopped, not playing)."""
        return self._audio_paused

    @property
    def is_stopped(self) -> bool:
        """Playback has been explicitly stopped (no active session)."""
        return self._stopped

    @property
    def is_looping(self) -> bool:
        """The current song is set to repeat instead of advancing the queue."""
        return self._looping

    @is_looping.setter
    def is_looping(self, value: bool) -> None:
        self._looping = bool(value)

    @property
    def is_finished(self) -> bool:
        """The current song's playback ended (EOF or unplayable file)."""
        if self._finished:
            return True
        # Decode failures may end the file without eof-reached ever flipping
        # (mpv drops to idle instead). Give loadfile a 2s grace period, then
        # treat an idle core during nominal playback as finished so the queue
        # can move on — mirrors the old "ffplay process died" detection.
        if (
            self._player is not None
            and self.is_active
            and self._play_start is not None
            and time.time() - self._play_start > 2.0
        ):
            try:
                return bool(self._player.idle_active)
            except Exception:
                return False
        return False

    # ── mpv lifecycle ────────────────────────────────────────────────────────

    def _ensure_player(self):
        """Create the persistent mpv instance on first use. Returns it or None."""
        if self._player is not None:
            return self._player
        mpv_mod = load_mpv()
        if mpv_mod is None:
            return None
        try:
            player = mpv_mod.MPV(
                vid="no",          # audio only
                idle="yes",        # survive stop/EOF; instance is reused
                keep_open="yes",   # hold at EOF so eof-reached fires reliably
            )
        except Exception:
            return None
        try:
            player.observe_property("eof-reached", self._on_eof_reached)
        except Exception:
            pass
        try:
            player.volume = self._volume
        except Exception:
            pass
        self._player = player
        return player

    def _on_eof_reached(self, _name, value) -> None:
        # Runs on mpv's event thread. eof-reached goes True at natural end of
        # file and back to False/None on the next loadfile/stop, so only a
        # truthy value marks the finish.
        if value:
            self._finished = True

    # ── Media keys ───────────────────────────────────────────────────────────

    def start_media_keys(self, dispatch_fn, on_stop=None, on_next=None, on_prev=None) -> None:
        try:
            from pynput import keyboard as pynput_kb
        except Exception as exc:
            print(f"Media keys unavailable (pynput import failed): {exc}")
            self._kb_listener = None
            return

        def on_press(key):
            if key == pynput_kb.Key.media_play_pause:
                dispatch_fn(self.toggle_pause)
            elif key == pynput_kb.Key.media_stop:
                dispatch_fn(on_stop if on_stop is not None else self.stop)
            elif key == pynput_kb.Key.media_next and on_next is not None:
                dispatch_fn(on_next)
            elif key == pynput_kb.Key.media_previous and on_prev is not None:
                dispatch_fn(on_prev)

        try:
            self._kb_listener = pynput_kb.Listener(on_press=on_press)
            self._kb_listener.daemon = True
            self._kb_listener.start()
        except Exception as exc:
            print(f"Media keys unavailable (listener failed to start): {exc}")
            self._kb_listener = None

    def stop_listener(self) -> None:
        if self._kb_listener:
            self._kb_listener.stop()

    # ── Transport ────────────────────────────────────────────────────────────

    def toggle_loop(self) -> None:
        self._looping = not self._looping

    def toggle_pause(self) -> None:
        if self._stopped:
            return
        player = self._player
        if player is None or self.playing_song is None or self._finished:
            self._audio_paused = False
            return
        try:
            if self._audio_paused:
                player.pause = False
                self._audio_paused = False
                if self._pause_start is not None:
                    self._paused_total += time.time() - self._pause_start
                    self._pause_start = None
            else:
                player.pause = True
                self._audio_paused = True
                self._pause_start = time.time()
        except Exception as exc:
            messagebox.showerror("Pause Failed", str(exc))

    def elapsed_seconds(self) -> float | None:
        if self._play_start is None:
            return None
        # Prefer mpv's real playback position; fall back to the wall-clock
        # estimate while the file is still loading (time-pos is None then).
        player = self._player
        if player is not None:
            try:
                pos = player.time_pos
            except Exception:
                pos = None
            if pos is not None:
                return max(0.0, float(pos))
        elapsed = time.time() - self._play_start - self._paused_total
        if self._pause_start is not None:
            elapsed -= time.time() - self._pause_start
        return max(0.0, elapsed)

    def _stop_mpv(self) -> None:
        player = self._player
        if player is not None:
            try:
                player.command("stop")
            except Exception:
                pass

    def stop_keep_song(self) -> None:
        """Stop audio but remember the current song and queue position."""
        self._audio_paused = False
        self._stopped = True
        self._finished = False
        self._play_start = None
        self._pause_start = None
        self._paused_total = 0.0
        self._stop_mpv()

    def stop(self) -> None:
        self._audio_paused = False
        self._stopped = True
        self._finished = False
        self.playing_song = None
        self._play_start = None
        self._pause_start = None
        self._paused_total = 0.0
        self.song_duration = None
        self._stop_mpv()

    def stop_and_wait(self, timeout: float = 2.0) -> None:
        """Stop and wait until mpv has actually released the audio file.

        Used before deleting a song's folder — the demuxer's open handle
        would otherwise make the delete fail on Windows.
        """
        self.stop()
        player = self._player
        if player is None:
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if player.idle_active:
                    return
            except Exception:
                return
            time.sleep(0.02)

    def set_volume(self, level: int) -> None:
        """Set volume 0-100. Applies live — no relaunch, works while paused."""
        level = max(0, min(100, level))
        self._volume = level
        player = self._player
        if player is not None:
            try:
                player.volume = level
            except Exception:
                pass

    def play(self, song: SongInfo) -> None:
        if not song.audio_path:
            messagebox.showwarning("Play Audio", "This song has no audio file.")
            return
        self.stop()
        self._stopped = False
        player = self._ensure_player()
        if player is None:
            self._play_without_mpv(song)
            return
        try:
            self._finished = False
            player.volume = self._volume
            player.pause = False  # keep-open leaves the player paused at EOF
            player.loadfile(str(song.audio_path))
        except Exception as exc:
            messagebox.showerror("Play Audio Failed", str(exc))
            # Let the queue tick treat this like a dead player and move on.
            self._finished = True
            return
        self.playing_song = song
        self._play_start = time.time()
        self._pause_start = None
        self._paused_total = 0.0
        self.song_duration = get_audio_duration(song.audio_path)
        self.session_id += 1

    def _play_without_mpv(self, song: SongInfo) -> None:
        """libmpv is unavailable — degrade the same way the ffplay-missing
        path used to: hand .ogg files to the OS default player (no in-app
        controls), otherwise explain what's missing and skip.

        If the DLL is simply absent (as opposed to present-but-broken or
        python-mpv not being installed), offer to download it first — once
        per run. The degrade-and-explain fallback below only actually runs if
        that offer is declined (now or already, earlier this run), the
        download/extraction fails, or the user declines the post-install
        restart; accepting the restart re-execs the process, so this song's
        playback attempt never gets a fallback at all."""
        ext = song.audio_path.suffix.lower()

        def _show_unavailable() -> None:
            if ext == ".ogg":
                try:
                    os.startfile(song.audio_path)
                    self._stopped = True
                    self.playing_song = None
                    self.song_duration = None
                    messagebox.showinfo(
                        "Play Audio",
                        f"{_mpv_unavailable_message()}\n\nHanded the file to your "
                        "system's default player instead. The in-app controls "
                        "(volume, pause, queue) won't apply to that playback.",
                    )
                except Exception as exc:
                    messagebox.showerror("Play Audio Failed", str(exc))
            else:
                messagebox.showwarning("Play Audio", _mpv_unavailable_message())
                # Mark finished so an active queue skips to the next song,
                # matching the old behavior when ffplay was missing.
                self._finished = True

        if dll_present():
            # DLL exists but is broken some other way (bad arch, python-mpv
            # not installed, etc.) — downloading a fresh copy wouldn't help.
            _show_unavailable()
        else:
            mpv_installer.offer_download_once(
                install_dir(),
                self._dispatch_fn or (lambda fn: fn()),
                status_cb=self._status_cb,
                on_unavailable=_show_unavailable,
            )
