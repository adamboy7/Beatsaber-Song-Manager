import os
import time
import ctypes
import ctypes.wintypes
import subprocess
from tkinter import messagebox

from libraries.audio_utils import find_ffplay, get_audio_duration
from libraries.song_data import SongInfo


def _create_kill_on_close_job():
    """Create a Windows Job Object that kills all assigned processes when the handle closes."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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
        # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        h = kernel32.OpenProcess(0x0101, False, pid)
        if h:
            kernel32.AssignProcessToJobObject(job, h)
            kernel32.CloseHandle(h)
    except Exception:
        pass


class MediaPlayer:
    def __init__(self):
        self._audio_proc: subprocess.Popen | None = None
        self._job = _create_kill_on_close_job()
        self._audio_paused: bool = False
        self._stopped: bool = False
        self._looping: bool = False
        self.playing_song: SongInfo | None = None
        self._kb_listener = None

        self._play_start: float | None = None
        self._pause_start: float | None = None
        self._paused_total: float = 0.0
        self.song_duration: float | None = None
        self._volume: int = 75
        self._volume_changed_while_paused: bool = False
        self.session_id: int = 0

    def start_media_keys(self, after_fn, on_stop=None, on_next=None, on_prev=None) -> None:
        try:
            from pynput import keyboard as pynput_kb
        except Exception as exc:
            print(f"Media keys unavailable (pynput import failed): {exc}")
            self._kb_listener = None
            return

        def on_press(key):
            if key == pynput_kb.Key.media_play_pause:
                after_fn(0, self.toggle_pause)
            elif key == pynput_kb.Key.media_stop:
                after_fn(0, on_stop if on_stop is not None else self.stop)
            elif key == pynput_kb.Key.media_next and on_next is not None:
                after_fn(0, on_next)
            elif key == pynput_kb.Key.media_previous and on_prev is not None:
                after_fn(0, on_prev)

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

    def toggle_loop(self) -> None:
        self._looping = not self._looping

    def toggle_pause(self) -> None:
        if self._stopped:
            return
        if not self._audio_proc or self._audio_proc.poll() is not None:
            self._audio_paused = False
            return
        try:
            import ctypes
            ntdll = ctypes.WinDLL("ntdll")
            kernel32 = ctypes.WinDLL("kernel32")
            handle = kernel32.OpenProcess(0x0800, False, self._audio_proc.pid)
            if not handle:
                # OpenProcess returned NULL (process exited between poll() and OpenProcess,
                # AV blocked the handle, insufficient privileges, etc.). Don't flip the
                # pause flag — the kernel will reject Nt(Suspend|Resume)Process(0) and we'd
                # end up out of sync with reality.
                messagebox.showerror(
                    "Pause Failed",
                    "Could not get a handle to the audio process. Try again.",
                )
                return
            try:
                if self._audio_paused:
                    status = ntdll.NtResumeProcess(handle)
                    if status == 0:  # STATUS_SUCCESS
                        self._audio_paused = False
                        if self._pause_start is not None:
                            self._paused_total += time.time() - self._pause_start
                            self._pause_start = None
                        if self._volume_changed_while_paused:
                            self._volume_changed_while_paused = False
                            elapsed = self.elapsed_seconds() or 0.0
                            if not (self.song_duration and elapsed > self.song_duration - 0.5):
                                song = self.playing_song
                                duration = self.song_duration
                                self._audio_proc.terminate()
                                self._audio_proc = None
                                if self._launch_ffplay(song, elapsed):
                                    self.song_duration = duration if duration is not None else get_audio_duration(song.audio_path)
                                else:
                                    self.playing_song = None
                                    self._play_start = None
                                    self._pause_start = None
                                    self._paused_total = 0.0
                                    self.song_duration = None
                else:
                    status = ntdll.NtSuspendProcess(handle)
                    if status == 0:
                        self._audio_paused = True
                        self._pause_start = time.time()
            finally:
                kernel32.CloseHandle(handle)
        except Exception as exc:
            messagebox.showerror("Pause Failed", str(exc))

    def elapsed_seconds(self) -> float | None:
        if self._play_start is None:
            return None
        elapsed = time.time() - self._play_start - self._paused_total
        if self._pause_start is not None:
            elapsed -= time.time() - self._pause_start
        return max(0.0, elapsed)

    def stop_keep_song(self) -> None:
        """Stop audio but remember the current song and queue position."""
        if self._audio_proc and self._audio_proc.poll() is None:
            self._audio_proc.terminate()
        self._audio_proc = None
        self._audio_paused = False
        self._stopped = True
        self._play_start = None
        self._pause_start = None
        self._paused_total = 0.0

    def stop(self) -> None:
        if self._audio_proc and self._audio_proc.poll() is None:
            self._audio_proc.terminate()
        self._audio_proc = None
        self._audio_paused = False
        self._stopped = True
        self.playing_song = None
        self._play_start = None
        self._pause_start = None
        self._paused_total = 0.0
        self.song_duration = None

    def stop_and_wait(self, timeout: float = 2.0) -> None:
        if self._audio_proc and self._audio_proc.poll() is None:
            proc = self._audio_proc
            self.stop()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        else:
            self.stop()

    def _launch_ffplay(self, song: SongInfo, seek: float = 0.0) -> bool:
        """Launch ffplay for song, optionally seeking. Returns True on success."""
        ffplay = find_ffplay()
        if not ffplay:
            return False
        cmd = [ffplay, "-nodisp", "-autoexit", "-volume", str(self._volume)]
        if seek > 0.5:
            cmd += ["-ss", f"{seek:.2f}"]
        cmd.append(str(song.audio_path))
        try:
            self._audio_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            assign_process_to_job(self._job, self._audio_proc.pid)
            self.playing_song = song
            self._play_start = time.time() - seek
            self._pause_start = None
            self._paused_total = 0.0
            return True
        except Exception as exc:
            messagebox.showerror("Play Audio Failed", str(exc))
            return False

    def set_volume(self, level: int) -> None:
        """Set volume 0-100. Restarts current song at its current position if actively playing."""
        level = max(0, min(100, level))
        self._volume = level
        if self._audio_paused and self.playing_song:
            self._volume_changed_while_paused = True
            return
        if (not self._audio_paused and not self._stopped
                and self._audio_proc and self._audio_proc.poll() is None
                and self.playing_song):
            elapsed = self.elapsed_seconds() or 0.0
            # Don't restart if very near the end
            if self.song_duration and elapsed > self.song_duration - 0.5:
                return
            song = self.playing_song
            duration = self.song_duration
            self._audio_proc.terminate()
            self._audio_proc = None
            self._audio_paused = False
            if self._launch_ffplay(song, elapsed):
                # Preserve previously known duration; re-probe if missing.
                self.song_duration = duration if duration is not None else get_audio_duration(song.audio_path)
            else:
                # Launch failed: leave player in a coherent "stopped" state instead of
                # claiming we still have a song playing with a stale duration.
                self.playing_song = None
                self._play_start = None
                self._pause_start = None
                self._paused_total = 0.0
                self.song_duration = None

    def play(self, song: SongInfo) -> None:
        if not song.audio_path:
            messagebox.showwarning("Play Audio", "This song has no audio file.")
            return
        self.stop()
        self._stopped = False
        if self._launch_ffplay(song):
            self.song_duration = get_audio_duration(song.audio_path)
            self.session_id += 1
        else:
            ext = song.audio_path.suffix.lower()
            if ext == ".ogg":
                # Degraded fallback: ffplay is unavailable, so hand the file
                # off to the OS default player. That player honors none of our
                # volume / pause / stop / queue controls and we have no
                # `_audio_proc` to poll, so keep the in-app state as "stopped"
                # to avoid a misleading player bar.
                try:
                    os.startfile(song.audio_path)
                    self._stopped = True
                    self.playing_song = None
                    self.song_duration = None
                    messagebox.showinfo(
                        "Play Audio",
                        "ffplay not found — handed the file to your system's default "
                        "player. The in-app controls (volume, pause, queue) won't apply "
                        "to that playback.",
                    )
                except Exception as exc:
                    messagebox.showerror("Play Audio Failed", str(exc))
            else:
                messagebox.showwarning(
                    "Play Audio",
                    "ffplay not found. Place ffplay.exe next to this script or add it to your PATH.",
                )
