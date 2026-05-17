import os
import time
import subprocess
from tkinter import messagebox

from libraries.audio_utils import find_ffplay, get_audio_duration
from libraries.song_data import SongInfo


class MediaPlayer:
    def __init__(self):
        self._audio_proc: subprocess.Popen | None = None
        self._audio_paused: bool = False
        self._looping: bool = False
        self.playing_song: SongInfo | None = None
        self._kb_listener = None

        self._play_start: float | None = None
        self._pause_start: float | None = None
        self._paused_total: float = 0.0
        self.song_duration: float | None = None

    def start_media_keys(self, after_fn, on_stop=None) -> None:
        from pynput import keyboard as pynput_kb

        def on_press(key):
            if key == pynput_kb.Key.media_play_pause:
                after_fn(0, self.toggle_pause)
            elif key == pynput_kb.Key.media_stop:
                after_fn(0, on_stop if on_stop is not None else self.stop)

        self._kb_listener = pynput_kb.Listener(on_press=on_press)
        self._kb_listener.daemon = True
        self._kb_listener.start()

    def stop_listener(self) -> None:
        if self._kb_listener:
            self._kb_listener.stop()

    def toggle_loop(self) -> None:
        self._looping = not self._looping

    def toggle_pause(self) -> None:
        if not self._audio_proc or self._audio_proc.poll() is not None:
            self._audio_paused = False
            return
        try:
            import ctypes
            ntdll = ctypes.WinDLL("ntdll")
            kernel32 = ctypes.WinDLL("kernel32")
            handle = kernel32.OpenProcess(0x1F0FFF, False, self._audio_proc.pid)
            if self._audio_paused:
                ntdll.NtResumeProcess(handle)
                self._audio_paused = False
                if self._pause_start is not None:
                    self._paused_total += time.time() - self._pause_start
                    self._pause_start = None
            else:
                ntdll.NtSuspendProcess(handle)
                self._audio_paused = True
                self._pause_start = time.time()
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

    def stop(self) -> None:
        if self._audio_proc and self._audio_proc.poll() is None:
            self._audio_proc.terminate()
        self._audio_proc = None
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

    def play(self, song: SongInfo) -> None:
        if not song.audio_path:
            messagebox.showwarning("Play Audio", "This song has no audio file.")
            return
        self.stop()
        ffplay = find_ffplay()
        if ffplay:
            try:
                self._audio_proc = subprocess.Popen(
                    [ffplay, "-nodisp", "-autoexit", str(song.audio_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.playing_song = song
                self._play_start = time.time()
                self._pause_start = None
                self._paused_total = 0.0
                self.song_duration = get_audio_duration(song.audio_path)
            except Exception as exc:
                messagebox.showerror("Play Audio Failed", str(exc))
        else:
            ext = song.audio_path.suffix.lower()
            if ext == ".ogg":
                try:
                    os.startfile(song.audio_path)
                except Exception as exc:
                    messagebox.showerror("Play Audio Failed", str(exc))
            else:
                messagebox.showwarning(
                    "Play Audio",
                    "ffplay not found. Place ffplay.exe next to this script or add it to your PATH.",
                )
