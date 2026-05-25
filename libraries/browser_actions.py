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

from libraries.constants import ACCENT_COLOR, BG_COLOR, TEXT_COLOR, SUBTEXT_COLOR
from libraries.song_data import SongInfo, save_custom_tags
from libraries.asset_editor import bak_files
from libraries.favorites import add_to_favorites, remove_from_favorites
from libraries.song_operations import (
    restore_song_files, replace_song_art, replace_song_audio, clear_song_score,
    save_song_info,
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
            song._parse()
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

    def _edit_song_info(self, song: SongInfo):
        from libraries.constants import BG_COLOR
        confirmed = messagebox.askokcancel(
            "Warning — Edit Info",
            "Modifying song info changes the data used to generate a song's hash in SongCore.\n\n"
            "This may:\n"
            "  • Alter how high scores are tracked\n"
            "  • Cause inconsistencies when importing or sharing playlists\n\n"
            "Continue?",
        )
        if not confirmed:
            return

        result = [None]

        dlg = tk.Toplevel(self)
        dlg.title("Edit Info")
        dlg.configure(bg=BG_COLOR)
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        form = tk.Frame(dlg, bg=BG_COLOR, padx=20, pady=16)
        form.pack(fill="both")

        fields = [
            ("Song Name", song.song_name),
            ("Artist",    song.author),
            ("Mapper",    song.mapper),
        ]
        entries: list[tk.Entry] = []
        for label_text, default in fields:
            row = tk.Frame(form, bg=BG_COLOR)
            row.pack(fill="x", pady=4)
            tk.Label(row, text=label_text, width=10, anchor="w",
                     font=("Segoe UI", 10), bg=BG_COLOR, fg=TEXT_COLOR).pack(side="left")
            entry = tk.Entry(row, font=("Segoe UI", 10),
                             bg="#1e1e1e", fg=TEXT_COLOR,
                             insertbackground=TEXT_COLOR,
                             relief="flat", bd=4, width=32)
            entry.insert(0, default)
            entry.pack(side="left", fill="x", expand=True)
            entries.append(entry)

        btn_frame = tk.Frame(dlg, bg=BG_COLOR)
        btn_frame.pack(pady=(4, 16))

        def _ok(_event=None):
            # Strip embedded newlines/carriage returns that can slip in via
            # paste and would cause the title label to render on two lines.
            result[0] = tuple(e.get().replace("\r", "").replace("\n", "") for e in entries)
            dlg.destroy()

        def _cancel(_event=None):
            dlg.destroy()

        tk.Button(btn_frame, text="OK", font=("Segoe UI", 10),
                  bg=ACCENT_COLOR, fg=TEXT_COLOR,
                  activebackground="#a01d90", activeforeground=TEXT_COLOR,
                  relief="flat", padx=14, pady=5,
                  command=_ok).pack(side="left", padx=6)
        tk.Button(btn_frame, text="Cancel", font=("Segoe UI", 10),
                  bg="#333333", fg=TEXT_COLOR,
                  activebackground="#444444", activeforeground=TEXT_COLOR,
                  relief="flat", padx=14, pady=5,
                  command=_cancel).pack(side="left", padx=6)

        dlg.bind("<Return>", _ok)
        dlg.bind("<Escape>", _cancel)

        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{x}+{y}")

        entries[0].focus_set()
        entries[0].select_range(0, "end")
        dlg.wait_window()

        if result[0] is None:
            return
        song_name, author, mapper = result[0]
        err = save_song_info(song, song_name, author, mapper)
        if err:
            messagebox.showerror("Edit Info Failed", err)
            return
        self._render_list()
        self.status_bar.config(text=f"Updated info for: {song.display_name}")

    # ── Context menus ─────────────────────────────────────────────────────────

    def _show_context_menu(self, event: tk.Event, song: SongInfo):
        is_fav = self._is_favorite(song)
        shift_held = bool(event.state & 0x1)
        baks = bak_files(song)
        menu = tk.Menu(self, tearoff=0, bg="#1e1e1e", fg=TEXT_COLOR,
                       activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR,
                       bd=0)
        if self._player_bar_visible:
            queue_empty = not self._queue
            if queue_empty:
                menu.add_command(label="Play",
                                 command=lambda: self._add_to_queue([song]),
                                 state="normal" if song.audio_path else "disabled")
            elif shift_held:
                menu.add_command(label="Play",
                                 command=lambda: self._add_to_queue_and_jump([song]),
                                 state="normal" if song.audio_path else "disabled")
            else:
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
            menu.add_command(label="Edit Info", foreground="#ff4444",
                             activeforeground="#ff4444",
                             command=lambda: self._edit_song_info(song))
            menu.add_command(label="Custom Tags…",
                             command=lambda: self._show_custom_tags_dialog([song]))
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
        if shift_held:
            menu.add_command(label="Custom Tags…",
                             command=lambda: self._show_custom_tags_dialog(songs))
            menu.add_separator()
        is_deletable = not any_favorited or shift_held
        menu.add_command(label="Delete",
                         command=lambda: self._delete_songs(songs, shift_held),
                         state="normal" if is_deletable else "disabled",
                         foreground="#ff5555" if is_deletable else SUBTEXT_COLOR)
        menu.tk_popup(event.x_root, event.y_root)

    # ── Custom tags dialog ────────────────────────────────────────────────────

    def _show_custom_tags_dialog(self, songs: list[SongInfo]):
        n = len(songs)
        multi = n > 1

        # Build initial tag state: {tag_name: count_of_songs_with_tag}
        all_tags: dict[str, int] = {}
        for song in songs:
            for t in song.custom_tags:
                all_tags[t] = all_tags.get(t, 0) + 1

        # Track changes as net additions/removals
        added: set[str] = set()
        removed: set[str] = set()

        dlg = tk.Toplevel(self)
        dlg.title("Custom Tags" if not multi else f"Custom Tags — {n} songs")
        dlg.configure(bg=BG_COLOR)
        dlg.resizable(False, False)
        dlg.grab_set()

        tk.Label(dlg, text="Tags:", font=("Segoe UI", 10, "bold"),
                 bg=BG_COLOR, fg=TEXT_COLOR, anchor="w").pack(
            fill="x", padx=16, pady=(14, 4))

        # Scrollable tag list
        list_frame = tk.Frame(dlg, bg="#1e1e1e", bd=1, relief="flat")
        list_frame.pack(fill="both", expand=True, padx=16)

        canvas = tk.Canvas(list_frame, bg="#1e1e1e", highlightthickness=0, width=320, height=160)
        scrollbar = tk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas, bg="#1e1e1e")
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_inner_resize(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(inner_id, width=canvas.winfo_width())

        inner.bind("<Configure>", _on_inner_resize)
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(inner_id, width=e.width))

        tag_rows: dict[str, tk.Frame] = {}

        def _add_tag_row(tag: str, count: int):
            row = tk.Frame(inner, bg="#1e1e1e")
            row.pack(fill="x", pady=1, padx=4)
            label_text = tag if not multi else f"{tag}  ({count}/{n})"
            tk.Label(row, text=label_text, font=("Segoe UI", 10),
                     bg="#1e1e1e", fg=TEXT_COLOR, anchor="w").pack(side="left", fill="x", expand=True)
            def _remove(t=tag):
                added.discard(t)
                removed.add(t)
                row.destroy()
                tag_rows.pop(t, None)
                canvas.update_idletasks()
                canvas.configure(scrollregion=canvas.bbox("all"))
            tk.Button(row, text="×", font=("Segoe UI", 10, "bold"),
                      bg="#1e1e1e", fg="#ff5555",
                      activebackground="#2a0033", activeforeground="#ff5555",
                      relief="flat", bd=0, padx=6,
                      command=_remove).pack(side="right")
            tag_rows[tag] = row

        for tag in sorted(all_tags):
            _add_tag_row(tag, all_tags[tag])

        # Add tag entry
        add_frame = tk.Frame(dlg, bg=BG_COLOR)
        add_frame.pack(fill="x", padx=16, pady=(10, 4))
        tk.Label(add_frame, text="Add tag:", font=("Segoe UI", 10),
                 bg=BG_COLOR, fg=TEXT_COLOR).pack(side="left")
        entry = tk.Entry(add_frame, font=("Segoe UI", 10),
                         bg="#1e1e1e", fg=TEXT_COLOR,
                         insertbackground=TEXT_COLOR,
                         relief="flat", bd=4, width=22)
        entry.pack(side="left", padx=(6, 4))

        def _do_add(_event=None):
            raw = entry.get().strip()
            if not raw:
                return
            # Reject whitespace-containing tags (can't be searched via {custom}:tag)
            if " " in raw:
                messagebox.showwarning("Custom Tags", "Tag names cannot contain spaces.", parent=dlg)
                return
            tag = raw
            if tag in tag_rows:
                entry.delete(0, "end")
                return
            removed.discard(tag)
            added.add(tag)
            all_tags[tag] = n
            _add_tag_row(tag, n)
            canvas.update_idletasks()
            canvas.configure(scrollregion=canvas.bbox("all"))
            entry.delete(0, "end")

        tk.Button(add_frame, text="Add", font=("Segoe UI", 10),
                  bg=ACCENT_COLOR, fg=TEXT_COLOR,
                  activebackground="#a01d90", activeforeground=TEXT_COLOR,
                  relief="flat", padx=10, pady=3,
                  command=_do_add).pack(side="left")
        entry.bind("<Return>", _do_add)

        # Save / Cancel
        btn_frame = tk.Frame(dlg, bg=BG_COLOR)
        btn_frame.pack(pady=(8, 16))

        def _save(_event=None):
            for song in songs:
                existing = set(song.custom_tags)
                existing -= removed
                existing |= added
                save_custom_tags(song.folder, existing)
                song.custom_tags = frozenset(existing)
            dlg.destroy()
            self._render_list()

        def _cancel(_event=None):
            dlg.destroy()

        tk.Button(btn_frame, text="Save", font=("Segoe UI", 10),
                  bg=ACCENT_COLOR, fg=TEXT_COLOR,
                  activebackground="#a01d90", activeforeground=TEXT_COLOR,
                  relief="flat", padx=14, pady=5,
                  command=_save).pack(side="left", padx=6)
        tk.Button(btn_frame, text="Cancel", font=("Segoe UI", 10),
                  bg="#333333", fg=TEXT_COLOR,
                  activebackground="#444444", activeforeground=TEXT_COLOR,
                  relief="flat", padx=14, pady=5,
                  command=_cancel).pack(side="left", padx=6)

        dlg.bind("<Escape>", _cancel)

        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{x}+{y}")
        entry.focus_set()
        dlg.wait_window()
