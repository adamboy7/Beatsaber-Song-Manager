import os
import signal
import subprocess
import time
import ctypes
import ctypes.wintypes
from libraries import dialogs
from libraries import platform_utils

from libraries.audio_utils import find_ffplay, get_audio_duration
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


def _win_suspend_resume(pid: int, suspend: bool) -> bool:
    """Suspend or resume every thread of ``pid`` via NtSuspend/ResumeProcess.

    Used to pause/resume the ffplay fallback backend (no live transport
    control, so freezing the process is how playback pauses). Mirrors the
    visualizer's spectrum-stream suspend. Best-effort: returns False on any
    failure.
    """
    try:
        kernel32 = ctypes.WinDLL("kernel32")
        ntdll = ctypes.WinDLL("ntdll")
        kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (
            ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD,
        )
        kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
        kernel32.CloseHandle.argtypes = (ctypes.wintypes.HANDLE,)
        fn = ntdll.NtSuspendProcess if suspend else ntdll.NtResumeProcess
        fn.restype = ctypes.c_long
        fn.argtypes = (ctypes.wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x0800, False, pid)  # PROCESS_SUSPEND_RESUME
        if not handle:
            return False
        try:
            return fn(handle) == 0
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def _suspend_process(pid: int) -> bool:
    """Freeze a child process (pause the ffplay backend). Cross-platform."""
    if platform_utils.IS_WINDOWS:
        return _win_suspend_resume(pid, suspend=True)
    try:
        os.kill(pid, signal.SIGSTOP)
        return True
    except Exception:
        return False


def _resume_process(pid: int) -> bool:
    """Unfreeze a child process (resume the ffplay backend). Cross-platform."""
    if platform_utils.IS_WINDOWS:
        return _win_suspend_resume(pid, suspend=False)
    try:
        os.kill(pid, signal.SIGCONT)
        return True
    except Exception:
        return False


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
        self._backend: str | None = None
        self._ffplay_proc: subprocess.Popen | None = None
        self._ffplay_suspended: bool = False
        self._job = None  # kill-on-close Job for the ffplay subprocess
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
        if self._backend == "ffplay":
            proc = self._ffplay_proc
            return proc is not None and proc.poll() is not None
        # Decode failures may end the file without eof-reached ever flipping
        # (mpv drops to idle instead). Give loadfile a 2s grace period, then
        # treat an idle core during nominal playback as finished so the queue
        # can move on.
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
        if self._backend == "ffplay":
            self._toggle_pause_ffplay()
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
            dialogs.show_error("Pause Failed", str(exc))

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

    def duration_seconds(self) -> float | None:
        """Total length of the current song in seconds, or None if unknown.

        Prefers mpv's own ``duration`` property — the demuxer knows the length
        of the file it already has open, so the progress bar works with no
        ffmpeg/ffprobe present. Falls back to the ffprobe-derived value
        (``song_duration``) for the brief window right after loadfile() while
        mpv is still parsing the header, and after stop() when the player has
        gone idle but callers still want the last known length.
        """
        player = self._player
        if player is not None:
            try:
                dur = player.duration
            except Exception:
                dur = None
            if dur:
                return float(dur)
        return self.song_duration

    def _stop_mpv(self) -> None:
        player = self._player
        if player is not None:
            try:
                player.command("stop")
            except Exception:
                pass

    def _stop_ffplay(self) -> None:
        """Terminate the ffplay subprocess and release its file handle.

        A suspended (paused) process is resumed first so terminate() can
        actually reap it. Only touches the process — timing state is managed
        by the caller.
        """
        proc = self._ffplay_proc
        self._ffplay_proc = None
        if proc is None:
            return
        if self._ffplay_suspended:
            _resume_process(proc.pid)
        self._ffplay_suspended = False
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

    def stop_keep_song(self) -> None:
        """Stop audio but remember the current song and queue position."""
        self._audio_paused = False
        self._stopped = True
        self._finished = False
        self._play_start = None
        self._pause_start = None
        self._paused_total = 0.0
        self._stop_mpv()
        self._stop_ffplay()

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
        self._stop_ffplay()
        self._backend = None

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
        """Set volume 0-100.

        On the mpv backend this applies live — no relaunch, works while paused.
        ffplay has no live volume control, so the fallback relaunches the
        subprocess at the current position with the new ``-volume``. The caller
        (the volume slider) debounces by 250ms, so a drag triggers at most one
        relaunch after it settles.
        """
        level = max(0, min(100, level))
        self._volume = level
        if self._backend == "ffplay":
            if self._ffplay_proc is not None and self.is_active:
                elapsed = self.elapsed_seconds() or 0.0
                was_paused = self._audio_paused
                self._stop_ffplay()
                if self._launch_ffplay(self.playing_song, elapsed) and was_paused:
                    # Stay frozen if we were paused before the volume change.
                    _suspend_process(self._ffplay_proc.pid)
                    self._ffplay_suspended = True
            return
        player = self._player
        if player is not None:
            try:
                player.volume = level
            except Exception:
                pass

    def play(self, song: SongInfo, warn_unavailable: bool = True) -> None:
        """Play ``song``. ``warn_unavailable`` controls what happens when no
        playback engine (libmpv or ffplay) is available: when True (an explicit
        play/toggle), the user is warned and offered the libmpv download; when
        False (an implicit auto-start, e.g. adding to an empty queue), playback
        is skipped silently so building a queue without an engine doesn't spam
        dialogs."""
        if not song.audio_path:
            if warn_unavailable:
                dialogs.show_warning("Play Audio", "This song has no audio file.")
            return
        self.stop()
        self._stopped = False
        player = self._ensure_player()
        if player is None:
            ffplay = find_ffplay()
            if ffplay is not None:
                self._play_with_ffplay(song, ffplay)
            else:
                self._play_without_mpv(song, warn_unavailable)
            return
        self._backend = "mpv"
        try:
            self._finished = False
            player.volume = self._volume
            player.pause = False  # keep-open leaves the player paused at EOF
            player.loadfile(str(song.audio_path))
        except Exception as exc:
            dialogs.show_error("Play Audio Failed", str(exc))
            # Let the queue tick treat this like a dead player and move on.
            self._finished = True
            return
        self.playing_song = song
        self._play_start = time.time()
        self._pause_start = None
        self._paused_total = 0.0
        self.song_duration = get_audio_duration(song.audio_path)
        self.session_id += 1

    # ── ffplay fallback backend ──────────────────────────────────────────────

    def _launch_ffplay(self, song: SongInfo, start: float) -> bool:
        """Start an ffplay subprocess for ``song`` seeked to ``start`` seconds.

        ffplay runs headless (``-nodisp``) and exits at end-of-file
        (``-autoexit``); the process is tied to a kill-on-close Job so it can't
        outlive the app. Returns True on launch, False (with an error dialog) on
        failure. Only manages the process — timing state is the caller's.
        """
        cmd = [
            find_ffplay(),
            "-nodisp", "-autoexit", "-loglevel", "quiet",
            "-volume", str(self._volume),
        ]
        if start > 0.1:
            cmd += ["-ss", f"{start:.2f}"]
        cmd += ["-i", str(song.audio_path)]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            dialogs.show_error("Play Audio Failed", str(exc))
            return False
        if self._job is None:
            self._job = _create_kill_on_close_job()
        assign_process_to_job(self._job, proc.pid)
        self._ffplay_proc = proc
        self._ffplay_suspended = False
        return True

    def _play_with_ffplay(self, song: SongInfo, ffplay: str) -> None:
        """Play ``song`` through the ffplay fallback backend."""
        self._backend = "ffplay"
        self._finished = False
        self._audio_paused = False
        self.song_duration = get_audio_duration(song.audio_path)
        self._play_start = time.time()
        self._pause_start = None
        self._paused_total = 0.0
        if not self._launch_ffplay(song, 0.0):
            # Couldn't start ffplay — mark finished so an active queue moves on.
            self._finished = True
            return
        self.playing_song = song
        self.session_id += 1

    def _toggle_pause_ffplay(self) -> None:
        """Pause/resume the ffplay backend by suspending its process.

        ffplay has no live transport, so freezing the process is the pause.
        Elapsed time stays correct via the wall-clock estimate
        (``_pause_start`` / ``_paused_total``) that ``elapsed_seconds`` already
        applies when there's no mpv time-pos.
        """
        proc = self._ffplay_proc
        if proc is None or self.playing_song is None or self._finished:
            self._audio_paused = False
            return
        try:
            if self._audio_paused:
                _resume_process(proc.pid)
                self._audio_paused = False
                self._ffplay_suspended = False
                if self._pause_start is not None:
                    self._paused_total += time.time() - self._pause_start
                    self._pause_start = None
            else:
                _suspend_process(proc.pid)
                self._audio_paused = True
                self._ffplay_suspended = True
                self._pause_start = time.time()
        except Exception as exc:
            dialogs.show_error("Pause Failed", str(exc))

    def _play_without_mpv(self, song: SongInfo, warn_unavailable: bool = True) -> None:
        """Neither libmpv nor ffplay is available — the last-resort path.

        (When ffplay is present, play() uses the ffplay backend instead and
        never reaches here.) There's no usable engine to play the audio, so
        this explains what's missing and skips the song. Beat Saber audio is
        `.egg`/`.ogg`, formats that usually aren't associated with any system
        player, so handing the file off to the OS isn't a reliable fallback —
        the app doesn't attempt it.

        If the DLL is simply absent (as opposed to present-but-broken or
        python-mpv not being installed), offer to download it first — once
        per run. On a successful install the DLL loads live (no restart), and
        ``on_ready`` retries this song straight away. The explain-and-skip
        fallback below only runs if that offer is declined (now or already,
        earlier this run) or the download/extraction fails.

        When ``warn_unavailable`` is False the song was auto-started implicitly
        (e.g. added to an empty queue), so there's nothing to warn about or offer
        — just mark it finished and let the queue move on silently."""

        if not warn_unavailable:
            self._finished = True
            return

        def _show_unavailable() -> None:
            dialogs.show_warning("Play Audio", _mpv_unavailable_message())
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
                on_ready=lambda: self.play(song),
            )
