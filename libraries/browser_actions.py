"""
Per-song actions and context menus for SongBrowser.

Groups together:
  • Favorites add/remove (single + multi-select).
  • File operations: restore from .bak, replace art, replace
    audio, delete folder, clear scores.
  • Clipboard helpers.
  • Single- and multi-song right-click context menus.
"""

from __future__ import annotations

import os
import shutil
import tkinter as tk
from tkinter import messagebox

from libraries.constants import ACCENT_COLOR, TEXT_COLOR, SUBTEXT_COLOR
from libraries.song_data import SongInfo
from libraries.asset_editor import bak_files
from libraries.favorites import add_to_favorites, remove_from_favorites
from libraries.song_operations import (
    restore_song_files, replace_song_art, replace_song_audio, clear_song_score,
)


class BrowserActionsMixin:
    """Favorites, song-file operations, and right-click menus."""

    # ── Favorites ─────────────────────────────────────────────────────────────

    def _add_to_favorites(self, song: SongInfo):
        if not self.player_dat_path:
            return
        if add_to_favorites(self.player_dat_path, song, self.favorite_ids):
            self._render_list()

    def _remove_from_favorites(self, song: SongInfo):
        if not self.player_dat_path:
            return
        if remove_from_favorites(self.player_dat_path, song, self.favorite_ids):
            self._render_list()

    def _add_to_favorites_multi(self, songs: list[SongInfo]):
        if not self.player_dat_path:
            return
        changed = False
        for song in songs:
            if not self._is_favorite(song):
                if add_to_favorites(self.player_dat_path, song, self.favorite_ids):
                    changed = True
        if changed:
            self._render_list()

    def _remove_from_favorites_multi(self, songs: list[SongInfo]):
        if not self.player_dat_path:
            return
        changed = False
        for song in songs:
            if self._is_favorite(song):
                if remove_from_favorites(self.player_dat_path, song, self.favorite_ids):
                    changed = True
        if changed:
            self._render_list()

    # ── File operations ───────────────────────────────────────────────────────

    def _restore_files(self, song: SongInfo):
        count, errors = restore_song_files(song)
        if count == 0:
            return
        if errors:
            messagebox.showerror("Restore Failed", "\n".join(errors))
        else:
            self._thumbnails.clear()
            self._render_list()
            self.status_bar.config(text=f"Restored {count} file(s) for: {song.display_name}")

    def _replace_art(self, song: SongInfo):
        if replace_song_art(self, song):
            self._thumbnails.clear()
            self._render_list()

    def _replace_audio(self, song: SongInfo):
        if replace_song_audio(self, song):
            self.status_bar.config(text=f"Audio replaced for: {song.display_name}")

    def _copy(self, text: str):
        self.clipboard_clear()
        self.clipboard_append(text)

    def _clear_score(self, song: SongInfo):
        if not self.player_dat_path:
            return
        if not messagebox.askyesno(
            "Clear Score",
            f'Clear all scores for "{song.display_name}"?\n\nThis cannot be undone (a backup will be made).',
            icon="warning", default="no",
        ):
            return
        result = clear_song_score(self.player_dat_path, song)
        if result is not None:
            removed, new_stats = result
            self.player_stats = new_stats
            self._render_list()
            self.status_bar.config(
                text=f"Cleared {removed} score entr{'y' if removed == 1 else 'ies'} for: {song.display_name}"
            )

    def _delete_song(self, song: SongInfo):
        msg = f'Delete "{song.display_name}"?\n\nThe folder will be removed from CustomLevels. Your scores will not be affected.'
        if not messagebox.askyesno("Delete Song", msg, icon="warning", default="no"):
            return
        if song is self._media_player.playing_song:
            self._media_player.stop_and_wait()
        try:
            shutil.rmtree(song.folder)
        except Exception as exc:
            messagebox.showerror("Delete Failed", str(exc))
            return
        self.songs    = [s for s in self.songs    if s is not song]
        self.filtered = [s for s in self.filtered if s is not song]
        self._selected_folders.discard(str(song.folder))
        self.selected_indices = {
            i for i, s in enumerate(self.filtered)
            if str(s.folder) in self._selected_folders
        }
        self.selected_index = max(self.selected_indices) if self.selected_indices else None
        self._thumbnails.clear()
        self._render_list()
        self.count_label.config(text=f"({len(self.songs)} songs)")
        self.status_bar.config(text=f"{len(self.filtered)} songs shown")

    def _delete_songs(self, songs: list[SongInfo], shift_held: bool):
        if any(self._is_favorite(s) for s in songs) and not shift_held:
            return
        count = len(songs)
        msg = (f'Delete {count} songs?\n\n'
               f'The folders will be removed from CustomLevels. Your scores will not be affected.')
        if not messagebox.askyesno("Delete Songs", msg, icon="warning", default="no"):
            return
        failed: list[tuple[SongInfo, Exception]] = []
        for song in songs:
            if song is self._media_player.playing_song:
                self._media_player.stop_and_wait()
            try:
                shutil.rmtree(song.folder)
            except Exception as exc:
                failed.append((song, exc))
        deleted_ids = {id(s) for s in songs} - {id(s) for s, _ in failed}
        deleted_folders = {str(s.folder) for s in songs if id(s) in deleted_ids}
        self.songs    = [s for s in self.songs    if id(s) not in deleted_ids]
        self.filtered = [s for s in self.filtered if id(s) not in deleted_ids]
        self._selected_folders -= deleted_folders
        self.selected_indices = {
            i for i, s in enumerate(self.filtered)
            if str(s.folder) in self._selected_folders
        }
        self.selected_index = max(self.selected_indices) if self.selected_indices else None
        self._thumbnails.clear()
        self._render_list()
        self.count_label.config(text=f"({len(self.songs)} songs)")
        self.status_bar.config(text=f"{len(self.filtered)} songs shown")
        if failed:
            errs = "\n".join(f"{s.display_name}: {exc}" for s, exc in failed)
            messagebox.showerror("Delete Failed", f"Failed to delete {len(failed)} song(s):\n{errs}")

    # ── Context menus ─────────────────────────────────────────────────────────

    def _show_context_menu(self, event: tk.Event, song: SongInfo):
        is_fav = self._is_favorite(song)
        shift_held = bool(event.state & 0x1)
        baks = bak_files(song)
        menu = tk.Menu(self, tearoff=0, bg="#1e1e1e", fg=TEXT_COLOR,
                       activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR,
                       bd=0)
        if self._player_bar_visible:
            menu.add_command(label="Add to Queue",
                             command=lambda: self._add_to_queue([song]),
                             state="normal" if song.audio_path else "disabled")
        else:
            menu.add_command(label="Play Audio",
                             command=lambda: self._play_queue([song]),
                             state="normal" if song.audio_path else "disabled")
        if is_fav:
            menu.add_command(label="Remove from Favorites",
                             command=lambda: self._remove_from_favorites(song),
                             state="normal" if self.player_dat_path else "disabled")
        else:
            menu.add_command(label="Add to Favorites",
                             command=lambda: self._add_to_favorites(song),
                             state="normal" if self.player_dat_path else "disabled")
        menu.add_separator()
        if shift_held:
            menu.add_command(label="Replace Art",
                             command=lambda: self._replace_art(song),
                             state="normal" if song.cover_path else "disabled")
            menu.add_command(label="Replace Audio",
                             command=lambda: self._replace_audio(song),
                             state="normal" if song.audio_path else "disabled")
            if baks:
                menu.add_command(label=f"Restore Files ({len(baks)})",
                                 command=lambda: self._restore_files(song))
        else:
            menu.add_command(label="Copy Link",
                             command=lambda: self._copy(f"https://beatsaver.com/maps/{song.song_id}"),
                             state="normal" if song.song_id else "disabled")
            menu.add_command(label="Copy Name", command=lambda: self._copy(song.display_name))
            menu.add_separator()
            menu.add_command(label="More from This Artist",
                             command=lambda: self.search_var.set(f"{{artist}}:{song.author}"),
                             state="normal" if song.author else "disabled")
            menu.add_command(label="More from This Mapper",
                             command=lambda: self.search_var.set(f"{{mapper}}:{song.mapper}"),
                             state="normal" if song.mapper else "disabled")
        menu.add_separator()
        menu.add_command(label="Open Folder…",
                         command=lambda: os.startfile(song.folder))
        menu.add_separator()
        if shift_held:
            menu.add_command(label="Clear Score",
                             command=lambda: self._clear_score(song),
                             state="normal" if self.player_dat_path else "disabled")
        is_deletable = not is_fav or shift_held
        menu.add_command(label="Delete",
                         command=lambda: self._delete_song(song),
                         state="normal" if is_deletable else "disabled",
                         foreground="#ff5555" if is_deletable else SUBTEXT_COLOR)
        menu.tk_popup(event.x_root, event.y_root)

    def _on_right_click(self, event: tk.Event, _idx: int, song: SongInfo):
        if len(self._selected_folders) > 1 and str(song.folder) in self._selected_folders:
            songs = [s for s in self.songs if str(s.folder) in self._selected_folders]
            self._show_context_menu_multi(event, songs)
        else:
            self._show_context_menu(event, song)

    def _show_context_menu_multi(self, event: tk.Event, songs: list[SongInfo]):
        shift_held = bool(event.state & 0x1)
        any_favorited = any(self._is_favorite(s) for s in songs)
        fav_state = "normal" if self.player_dat_path else "disabled"
        menu = tk.Menu(self, tearoff=0, bg="#1e1e1e", fg=TEXT_COLOR,
                       activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR,
                       bd=0)
        if self._player_bar_visible:
            menu.add_command(label="Add to Queue",
                             command=lambda: self._add_to_queue(songs))
        else:
            has_audio = any(s.audio_path for s in songs)
            menu.add_command(label="Play",
                             state="normal" if has_audio else "disabled",
                             command=lambda: self._play_queue(songs))
        menu.add_separator()
        menu.add_command(label="Add to Favorites",
                         command=lambda: self._add_to_favorites_multi(songs),
                         state=fav_state)
        menu.add_command(label="Remove from Favorites",
                         command=lambda: self._remove_from_favorites_multi(songs),
                         state=fav_state)
        menu.add_separator()
        menu.add_command(label="Share Playlist",
                         command=lambda: self._share_playlist(songs),
                         state="normal")
        menu.add_separator()
        is_deletable = not any_favorited or shift_held
        menu.add_command(label="Delete",
                         command=lambda: self._delete_songs(songs, shift_held),
                         state="normal" if is_deletable else "disabled",
                         foreground="#ff5555" if is_deletable else SUBTEXT_COLOR)
        menu.tk_popup(event.x_root, event.y_root)
