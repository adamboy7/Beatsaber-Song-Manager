"""
Playback / queue / player-bar behavior for SongBrowser.

Manages the audio queue (``self._queue`` / ``self._queue_index``),
the now-playing bar widgets, the idle braille animation, the
periodic tick that advances the queue, and the right-click player
context menu (Play/Pause/Stop/Shuffle/Loop/Next/Prev).

Also owns the View-toggle handlers for player visibility, queue
loop, and shuffle, and the spacebar pause hotkey.
"""

from __future__ import annotations

import random
import tkinter as tk
from tkinter import messagebox

from libraries.constants import ACCENT_COLOR, TEXT_COLOR
from libraries.song_data import SongInfo


_IDLE_BRAILLE = "⠠⠏⠇⠁⠽ ⠞⠓⠁⠞ ⠎⠕⠝⠛   "


class BrowserPlaybackMixin:
    """Audio playback, queue management, and player-bar UI."""

    # ── View toggles tied to playback ─────────────────────────────────────────

    def _toggle_keep_player_visible(self):
        self._keep_player_visible = self._keep_player_visible_var.get()
        if self._keep_player_visible and not self._player_bar_visible:
            if self._media_player._audio_proc is not None:
                self._show_player_bar(self._media_player.playing_song)
            else:
                self._show_player_bar_idle(None, None)
                self._player_bar_frame.pack(fill="x", padx=16, pady=(0, 4), before=self.status_bar)
                self._player_bar_visible = True
        elif not self._keep_player_visible:
            self._hide_player_bar()

    def _toggle_loop_queue(self):
        self._loop_queue = self._loop_queue_var.get()

    def _toggle_loop(self):
        self._media_player.toggle_loop()
        self._loop_var.set(self._media_player._looping)
        self._update_status_icon()

    def _toggle_shuffle_queue(self):
        self._shuffle_queue = not self._shuffle_queue
        if not self._shuffle_queue:
            self._last_shuffle_index = None
        self._shuffle_queue_var.set(self._shuffle_queue)

    def _shuffle_queue_inplace(self):
        """Shuffle the queue order, keeping the currently-playing song tracked."""
        queue = self._queue
        if len(queue) < 2:
            return
        curr = self._queue_index
        # Permute by indices so duplicate SongInfo references don't confuse the
        # "where is the playing song now?" lookup.
        perm = list(range(len(queue)))
        random.shuffle(perm)
        queue[:] = [queue[i] for i in perm]
        if 0 <= curr < len(queue):
            self._queue_index = perm.index(curr)
        self._notify_queue_window()

    def _update_status_icon(self):
        mp = self._media_player
        looping = mp._looping
        shuffling = self._shuffle_queue
        self._loop_var.set(looping)
        if looping and shuffling:
            self._loop_icon_label.config(text="↻")
            self._shuffle_icon_label.config(text="⇄")
        elif looping:
            self._loop_icon_label.config(text="")
            self._shuffle_icon_label.config(text="↻")
        elif shuffling:
            self._loop_icon_label.config(text="")
            self._shuffle_icon_label.config(text="⇄")
        else:
            self._loop_icon_label.config(text="")
            self._shuffle_icon_label.config(text="")

    def _on_space(self, *_):
        if self.focus_get() is self.search_entry:
            return
        self._media_player.toggle_pause()

    def _on_player_play_btn_click(self, _event=None):
        mp = self._media_player
        if not self._queue:
            return
        if mp._stopped:
            self._play_audio(self._queue[self._queue_index])
        else:
            mp.toggle_pause()

    def _refresh_player_play_btn(self):
        mp = self._media_player
        has_queue = bool(self._queue)
        is_playing = has_queue and not mp._stopped and not mp._audio_paused
        self._player_play_btn.config(
            text="⏸" if is_playing else "▶",
            state="normal" if has_queue else "disabled",
            cursor="hand2" if has_queue else "",
        )
        if mp._looping or not self._queue:
            can_next = can_prev = False
        else:
            can_next = (
                self._queue_index + 1 < len(self._queue)
                or (self._shuffle_queue and len(self._queue) >= 2)
                or self._loop_queue
            )
            can_prev = self._queue_index > 0 or self._loop_queue
        self._player_next_btn.config(
            state="normal" if can_next else "disabled",
            fg=TEXT_COLOR if can_next else "#555577",
        )
        self._player_back_btn.config(
            state="normal" if can_prev else "disabled",
            fg=TEXT_COLOR if can_prev else "#555577",
        )

    def _toggle_mute(self) -> None:
        if self._vol_muted:
            self._vol_muted = False
            level = self._vol_pre_mute if self._vol_pre_mute > 0 else 75
            self._volume_var.set(level)
            self._vol_icon_label.config(text="🔊")
            self._volume_label.config(text=f"{level}%")
            self._draw_vol_canvas()
            self._media_player.set_volume(level)
        else:
            self._vol_pre_mute = self._volume_var.get()
            self._vol_muted = True
            self._volume_var.set(0)
            self._vol_icon_label.config(text="🔇")
            self._volume_label.config(text="0%")
            self._draw_vol_canvas()
            self._media_player.set_volume(0)

    def _on_volume_change(self, level: int) -> None:
        self._volume_label.config(text=f"{level}%")
        pending = getattr(self, "_volume_apply_id", None)
        if pending:
            self.after_cancel(pending)
        self._volume_apply_id = self.after(250, lambda: self._media_player.set_volume(level))

    # ── Play / queue ──────────────────────────────────────────────────────────

    def _play_audio(self, song: SongInfo):
        if song is not self._media_player.playing_song:
            self._media_player._looping = False
        self._media_player.play(song)
        self._show_player_bar(song)
        self._start_player_tick()

    def _play_queue(self, songs: list[SongInfo]) -> None:
        playable = [s for s in songs if s.audio_path]
        if not playable:
            return
        self._queue = playable
        self._queue_index = 0
        self._play_audio(playable[0])
        self._notify_queue_window()

    def _add_to_queue(self, songs: list[SongInfo]) -> None:
        playable = [s for s in songs if s.audio_path]
        if not playable:
            return
        self._queue.extend(playable)
        if self._media_player.playing_song is None and self._player_bar_visible:
            self._queue_index = len(self._queue) - len(playable)
            self._play_audio(self._queue[self._queue_index])
        self._notify_queue_window()

    def _add_to_queue_and_jump(self, songs: list[SongInfo]) -> None:
        """Append songs to the queue and immediately jump to the first one."""
        playable = [s for s in songs if s.audio_path]
        if not playable:
            return
        insert_index = len(self._queue)
        self._queue.extend(playable)
        self._queue_index = insert_index
        self._play_audio(self._queue[insert_index])
        self._notify_queue_window()

    def _queue_next(self) -> None:
        if self._media_player._looping:
            return
        if self._shuffle_queue and len(self._queue) >= 2:
            candidates = [i for i in range(len(self._queue)) if i != self._queue_index]
            next_idx = random.choice(candidates)
            if next_idx == self._last_shuffle_index and len(candidates) > 1:
                candidates.remove(next_idx)
                next_idx = random.choice(candidates)
            self._last_shuffle_index = next_idx
            self._queue_index = next_idx
            self._play_audio(self._queue[next_idx])
            return
        next_idx = self._queue_index + 1
        if next_idx < len(self._queue):
            self._queue_index = next_idx
            self._play_audio(self._queue[next_idx])
        elif self._loop_queue and self._queue:
            self._queue_index = 0
            self._play_audio(self._queue[0])

    def _queue_prev(self) -> None:
        if self._media_player._looping:
            return
        if self._queue_index > 0:
            self._queue_index -= 1
            self._play_audio(self._queue[self._queue_index])
        elif self._loop_queue and self._queue:
            if self._shuffle_queue and len(self._queue) >= 2:
                candidates = [i for i in range(len(self._queue)) if i != self._queue_index]
                prev_idx = random.choice(candidates)
                self._last_shuffle_index = prev_idx
                self._queue_index = prev_idx
                self._play_audio(self._queue[prev_idx])
            else:
                last_idx = len(self._queue) - 1
                self._queue_index = last_idx
                self._play_audio(self._queue[last_idx])

    # ── Player bar ────────────────────────────────────────────────────────────

    def _show_player_bar_idle(self, song: SongInfo | None, duration: float | None) -> None:
        if song is None:
            self._player_time_label.config(text="--:--")
            self._player_progress["value"] = 0
            self._start_idle_animation()
            return
        self._stop_idle_animation()
        name = song.display_name or song.song_name or "Unknown"
        self._player_name_label.config(text=f"■  {name}")
        if duration:
            d_min, d_sec = divmod(int(duration), 60)
            self._player_time_label.config(text=f"{d_min}:{d_sec:02d} / {d_min}:{d_sec:02d}")
            self._player_progress["value"] = 100.0
        else:
            self._player_time_label.config(text="--:--")

    def _show_player_bar(self, song: SongInfo):
        self._stop_idle_animation()
        name = song.display_name or song.song_name or "Unknown"
        self._player_name_label.config(text=f"▶  {name}")
        self._update_status_icon()
        self._player_time_label.config(text="0:00")
        self._player_progress["value"] = 0
        if self._keep_player_visible:
            self._player_bar_frame.pack(fill="x", padx=16, pady=(0, 4), before=self.status_bar)
            self._player_bar_visible = True

    def _hide_player_bar(self):
        self._stop_idle_animation()
        self._player_bar_frame.pack_forget()
        self._player_bar_visible = False

    def _start_idle_animation(self):
        if self._idle_anim_id:
            self.after_cancel(self._idle_anim_id)
        self._idle_anim_frame = 0
        self._tick_idle_anim()

    def _tick_idle_anim(self):
        n = len(_IDLE_BRAILLE)
        frame = self._idle_anim_frame % n
        window = (_IDLE_BRAILLE * 2)[frame:frame + 4]
        self._player_name_label.config(
            text=f"■  Add a song to Queue to begin  {window}"
        )
        self._idle_anim_frame += 1
        self._idle_anim_id = self.after(150, self._tick_idle_anim)

    def _stop_idle_animation(self):
        if self._idle_anim_id:
            self.after_cancel(self._idle_anim_id)
            self._idle_anim_id = None

    def _stop_playback(self):
        """Stop audio but keep queue and remember position. Disables play/pause resume."""
        if self._player_tick_id:
            self.after_cancel(self._player_tick_id)
            self._player_tick_id = None
        song = self._media_player.playing_song
        self._media_player.stop_keep_song()
        if self._keep_player_visible:
            if song:
                name = song.display_name or song.song_name or "Unknown"
                self._player_name_label.config(text=f"■  {name}")
                self._update_status_icon()
            else:
                self._show_player_bar_idle(None, None)
        self._refresh_player_play_btn()

    def _confirm_clear_queue(self):
        if not messagebox.askyesno(
            "Clear Queue",
            "Stop playback and clear the entire queue?",
            icon="warning",
            default="no",
        ):
            return
        self._stop_player()

    def _stop_player(self):
        """Fully stop playback and clear the queue."""
        if self._player_tick_id:
            self.after_cancel(self._player_tick_id)
            self._player_tick_id = None
        self._media_player.stop()
        if self._keep_player_visible:
            self._show_player_bar_idle(None, None)
        else:
            self._hide_player_bar()
        self._queue.clear()
        self._queue_index = -1
        self._refresh_player_play_btn()
        self._notify_queue_window()

    def _stop_audio_keep_queue(self):
        """Stop audio and lose queue position (e.g. playing song was deleted)."""
        if self._player_tick_id:
            self.after_cancel(self._player_tick_id)
            self._player_tick_id = None
        self._media_player.stop()
        if self._keep_player_visible:
            self._show_player_bar_idle(None, None)
        else:
            self._hide_player_bar()
        self._queue_index = -1

    def _show_player_context_menu(self, event: tk.Event):
        mp = self._media_player
        stopped = mp._stopped
        paused = mp._audio_paused
        queue_empty = not self._queue

        if stopped and 0 <= self._queue_index < len(self._queue):
            play_label = "Play"
            play_cmd = lambda: self._play_audio(self._queue[self._queue_index])
            play_state = "normal"
        elif paused:
            play_label = "Play"
            play_cmd = mp.toggle_pause
            play_state = "normal"
        elif queue_empty:
            play_label = "Pause"
            play_cmd = mp.toggle_pause
            play_state = "disabled"
        else:
            play_label = "Pause"
            play_cmd = mp.toggle_pause
            play_state = "normal"

        loop_var = tk.BooleanVar(value=mp._looping)

        menu = tk.Menu(self, tearoff=0, bg="#1e1e1e", fg=TEXT_COLOR,
                       activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR, bd=0)
        menu.add_command(label="View Queue",
                         command=self._open_queue_window)
        menu.add_separator()
        menu.add_command(label=play_label, command=play_cmd, state=play_state)
        menu.add_command(label="Stop", command=self._stop_playback,
                         state="disabled" if (stopped or queue_empty) else "normal")
        shuffle_var = tk.BooleanVar(value=self._shuffle_queue)
        can_shuffle = len(self._queue) >= 2
        menu.add_checkbutton(
            label="Shuffle", variable=shuffle_var,
            command=self._toggle_shuffle_queue,
            selectcolor=ACCENT_COLOR,
            state="normal" if can_shuffle else "disabled",
        )
        menu.add_checkbutton(
            label="Loop", variable=loop_var, command=self._toggle_loop,
            selectcolor=ACCENT_COLOR,
        )
        can_next = not mp._looping and (
            self._queue_index + 1 < len(self._queue)
            or (self._shuffle_queue and len(self._queue) >= 2)
            or (self._loop_queue and bool(self._queue))
        )
        can_prev = not mp._looping and (
            self._queue_index > 0
            or (self._loop_queue and bool(self._queue))
        )
        menu.add_separator()
        menu.add_command(label="Next",
                         state="normal" if can_next else "disabled",
                         command=self._queue_next)
        menu.add_command(label="Previous",
                         state="normal" if can_prev else "disabled",
                         command=self._queue_prev)
        menu.add_separator()
        if queue_empty:
            menu.add_command(label="Clear Queue", state="disabled")
        else:
            menu.add_command(
                label="Clear Queue",
                foreground="#ff4444",
                activeforeground="#ff4444",
                command=self._confirm_clear_queue,
            )
        menu.tk_popup(event.x_root, event.y_root)

    # ── Periodic tick ─────────────────────────────────────────────────────────

    def _start_player_tick(self):
        if self._player_tick_id:
            self.after_cancel(self._player_tick_id)
        self._player_tick_id = self.after(500, self._tick_player)

    def _tick_player(self):
        mp = self._media_player
        if mp._stopped:
            self._player_tick_id = None
            return
        proc = mp._audio_proc
        if proc is None or proc.poll() is not None:
            if mp._looping and mp.playing_song:
                self._play_audio(mp.playing_song)
                return
            if self._shuffle_queue and len(self._queue) >= 2:
                candidates = [i for i in range(len(self._queue)) if i != self._queue_index]
                next_idx = random.choice(candidates)
                if next_idx == self._last_shuffle_index and len(candidates) > 1:
                    candidates.remove(next_idx)
                    next_idx = random.choice(candidates)
                self._last_shuffle_index = next_idx
                self._queue_index = next_idx
                self._play_audio(self._queue[next_idx])
                return
            next_idx = self._queue_index + 1
            if 0 <= next_idx < len(self._queue):
                self._queue_index = next_idx
                self._play_audio(self._queue[next_idx])
                return
            if self._loop_queue and self._queue:
                self._queue_index = 0
                self._play_audio(self._queue[0])
                return
            last_song = mp.playing_song
            last_duration = mp.song_duration
            mp.stop()
            self._show_player_bar_idle(last_song, last_duration)
            self._refresh_player_play_btn()
            self._player_tick_id = None
            return

        elapsed = mp.elapsed_seconds() or 0.0
        duration = mp.song_duration
        paused = mp._audio_paused

        icon = "▌▌" if paused else "▶"
        song = mp.playing_song
        name = (song.display_name or song.song_name or "Unknown") if song else ""
        self._player_name_label.config(text=f"{icon}  {name}")
        self._update_status_icon()
        self._refresh_player_play_btn()

        e_min, e_sec = divmod(int(elapsed), 60)
        if duration:
            d_min, d_sec = divmod(int(duration), 60)
            self._player_time_label.config(text=f"{e_min}:{e_sec:02d} / {d_min}:{d_sec:02d}")
            pct = min(100.0, elapsed / duration * 100)
            self._player_progress["value"] = pct
        else:
            self._player_time_label.config(text=f"{e_min}:{e_sec:02d}")
            self._player_progress["value"] = 0

        self._player_tick_id = self.after(500, self._tick_player)
