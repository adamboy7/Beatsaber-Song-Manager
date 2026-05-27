"""
Playlist handling and sub-window orchestration for SongBrowser.

Includes:
  • Song-library loading (background thread + on-loaded handler).
  • View-filter toggles and the favorites predicate.
  • Saving the current selection as a .bplist ("share playlist").
  • Drag-and-drop / File→Open handling for .bplist files,
    including auto-installing missing songs via Mod Assistant.
  • Opening and refreshing the QueueWindow / PlaylistArtWindow
    sub-windows.
"""

from __future__ import annotations

import io
import json
import base64
import os
import random
import tempfile
import threading
import urllib.request
import tkinter as tk
from tkinter import messagebox
from tkinterdnd2 import DND_FILES
from pathlib import Path
from PIL import Image

from libraries.constants import ACCENT_COLOR, TEXT_COLOR
from libraries.song_data import SongInfo, load_songs, load_song_hashes, compute_song_hash
from libraries.player_data import (
    song_level_ids, load_favorites, load_player_stats,
)
from libraries.playlist_installer import PlaylistInstaller
from libraries.queue_window import QueueWindow
from libraries.playlist_art_window import PlaylistArtWindow


def _ask_overwrite_or_append(parent: tk.Misc, anchor: tk.Misc | None = None) -> str:
    """3-button dialog for non-empty queue drop. Returns 'overwrite', 'append', or ''."""
    result: dict[str, str] = {"choice": ""}

    dlg = tk.Toplevel(parent)
    dlg.title("Queue Not Empty")
    dlg.configure(bg="#0d0d1a")
    dlg.resizable(False, False)
    dlg.transient(parent)
    dlg.grab_set()
    _icon_source = anchor or parent
    _dlg_icon = getattr(_icon_source, '_icon', None)
    if _dlg_icon is None:
        try:
            _dlg_icon = tk.PhotoImage(file=Path(__file__).parent.parent / "Warning.png")
        except Exception:
            pass
    if _dlg_icon is not None:
        try:
            dlg.iconphoto(False, _dlg_icon)
            dlg._dlg_icon = _dlg_icon
        except Exception:
            pass

    tk.Label(
        dlg,
        text="The queue already has songs.\nWhat would you like to do?",
        font=("Segoe UI", 10),
        bg="#0d0d1a", fg=TEXT_COLOR,
        justify="center",
        padx=20, pady=16,
    ).pack()

    btn_frame = tk.Frame(dlg, bg="#0d0d1a")
    btn_frame.pack(pady=(0, 16), padx=20)

    def choose(val: str):
        result["choice"] = val
        dlg.destroy()

    for label, val, bg in [
        ("Overwrite", "overwrite", ACCENT_COLOR),
        ("Append",    "append",    "#2a2a3a"),
        ("Cancel",    "",          "#2a2a3a"),
    ]:
        tk.Button(
            btn_frame, text=label,
            font=("Segoe UI", 9),
            bg=bg, fg=TEXT_COLOR,
            activebackground="#7a44c0", activeforeground=TEXT_COLOR,
            bd=0, padx=14, pady=6,
            command=lambda v=val: choose(v),
        ).pack(side="left", padx=4)

    dlg.update_idletasks()
    target = anchor or parent
    x = target.winfo_rootx() + (target.winfo_width()  - dlg.winfo_width())  // 2
    y = target.winfo_rooty() + (target.winfo_height() - dlg.winfo_height()) // 2
    dlg.geometry(f"+{x}+{y}")

    dlg.wait_window()
    return result["choice"]


class BrowserPlaylistsMixin:
    """Song loading, view filters, .bplist I/O, and sub-window
    orchestration."""

    # ── Song loading ──────────────────────────────────────────────────────────

    def _refresh(self, *_):
        if self.player_dat_path:
            self.player_stats  = load_player_stats(self.player_dat_path)
            self.favorite_ids  = load_favorites(self.player_dat_path)
        self.status_bar.config(text="Refreshing…")
        self._load_async()

    def _load_async(self):
        # Tag each background load with a generation counter so that if the user
        # hammers F5 (or an install completes mid-reload) we can ignore stale
        # results that would otherwise wipe out the newer load's state.
        self._load_gen = getattr(self, "_load_gen", 0) + 1
        gen = self._load_gen

        def worker():
            songs = load_songs(self.custom_levels)
            hashes = load_song_hashes(self.custom_levels)
            for song in songs:
                song.song_hash = hashes.get(song.folder.name, "")
            self.after(0, lambda: self._maybe_apply_loaded(gen, songs))

        threading.Thread(target=worker, daemon=True).start()

    def _maybe_apply_loaded(self, gen: int, songs: list[SongInfo]):
        if gen != getattr(self, "_load_gen", 0):
            return  # superseded by a newer load; drop the stale result
        self._on_loaded(songs)

    def _on_loaded(self, songs: list[SongInfo]):
        self.songs = songs
        self._selected_folders.clear()
        self.selected_indices.clear()
        self.selected_index = None
        self.count_label.config(text=f"({len(songs)} songs)")
        self._on_search()  # re-applies search bar contents; resolves _pending_install_id

        # Startup hooks fire once on initial load.  The CLI contract makes
        # `playlist + --randomAdd` headless, so in practice only one of these
        # two paths fires here — but we keep both checks defensive so that a
        # caller wiring up both doesn't silently lose the loaded playlist.
        startup_playlist_loaded = False
        if self._startup_playlist is not None:
            path, self._startup_playlist = self._startup_playlist, None
            self._load_playlist_to_queue(str(path), anchor=self)
            startup_playlist_loaded = True

        if self._startup_random_groups:
            groups, self._startup_random_groups = self._startup_random_groups, []
            playable = [s for s in songs if s.audio_path]
            if not playable:
                print("Warning: no playable songs found in library.")
            else:
                from libraries.browser_pagination import filter_songs, pick_random_songs
                all_picks: list = []
                excluded: set = set()
                for count, filter_str in groups:
                    candidates = [s for s in playable if s.folder not in excluded]
                    filtered = None
                    if filter_str:
                        filtered = filter_songs(candidates, filter_str, self.player_stats, self.favorite_ids)
                        if not filtered:
                            print(f"Warning: filter '{filter_str}' matched no songs; falling back to unfiltered picks.")
                    picks = pick_random_songs(filtered, candidates, count)
                    all_picks.extend(picks)
                    excluded.update(s.folder for s in picks)
                if all_picks:
                    # If a startup playlist was just loaded, append picks to
                    # it rather than overwriting (was: _play_queue silently
                    # discarded the loaded playlist).
                    if startup_playlist_loaded:
                        self._add_to_queue(all_picks)
                    else:
                        self._play_queue(all_picks)

        # Shuffle the resulting queue regardless of source (playlist load,
        # random picks, or both).  Previously this only fired inside the
        # random-picks branch, so `--shuffle` with a playlist-only startup
        # was a silent no-op.
        if self._startup_shuffle:
            self._startup_shuffle = False
            self._shuffle_queue_inplace()
            self._queue_index = 0
            if self._queue:
                self._play_audio(self._queue[0])
                self._notify_queue_window()

    # ── View filters ──────────────────────────────────────────────────────────

    def _is_favorite(self, song: SongInfo) -> bool:
        return any(lid in self.favorite_ids for lid in song_level_ids(song))

    def _apply_view_filters(self, songs: list[SongInfo]) -> list[SongInfo]:
        if self._favorites_only:
            songs = [s for s in songs if self._is_favorite(s)]
        if self._hide_favorites:
            songs = [s for s in songs if not self._is_favorite(s)]
        return songs

    def _toggle_favorites_only(self):
        self._favorites_only = self._favorites_only_var.get()
        self._on_search()

    def _toggle_hide_favorites(self):
        if self._favorites_only and self._hide_favorites_var.get():
            self._favorites_only = False
            self._favorites_only_var.set(False)
        self._hide_favorites = self._hide_favorites_var.get()
        self._on_search()

    # ── Playlist export ───────────────────────────────────────────────────────

    def _share_playlist(self, songs: list[SongInfo], parent: tk.Misc | None = None) -> None:
        invalid = [s for s in songs if not s.song_hash]
        valid = [s for s in songs if s.song_hash]

        if invalid:
            names = "\n".join(f"  • {s.display_name}" for s in invalid)
            if not valid:
                messagebox.showerror(
                    "Cannot Create Playlist",
                    "None of the selected songs have a hash — they may not have been "
                    "loaded by Beat Saber yet.\n\n" + names,
                )
                return
            proceed = messagebox.askyesno(
                "Invalid Songs",
                f"{len(invalid)} song(s) have no hash and will be skipped:\n\n"
                + names
                + "\n\nContinue with the remaining "
                + str(len(valid))
                + " song(s)?",
            )
            if not proceed:
                return

        # Detect songs whose Info.dat has been edited (a .bak backup exists).
        edited_baks: dict[Path, Path] = {}
        for s in valid:
            for bak_name in ("Info.dat.bak", "info.dat.bak", "INFO.DAT.bak"):
                bak = s.folder / bak_name
                if bak.exists():
                    edited_baks[s.folder] = bak
                    break

        if edited_baks:
            edited_names = "\n".join(
                f"  • {s.display_name}" for s in valid if s.folder in edited_baks
            )
            messagebox.showwarning(
                "Edited Songs Detected",
                f"{len(edited_baks)} song(s) have a modified Info.dat "
                f"(original backed up as .bak):\n\n{edited_names}\n\n"
                "Modifying Info.dat changes the SongCore hash used to identify and "
                "download songs — the edited version will not be recognised by "
                "other tools or players.\n\n"
                "The playlist will use a best-effort hash recalculated from the "
                "original Info.dat file.",
                parent=parent or self,
            )

        # Build hash overrides from .bak originals for edited songs.
        hash_overrides: dict[Path, str] = {}
        for folder, bak in edited_baks.items():
            h = compute_song_hash(folder, bak)
            if h:
                hash_overrides[folder] = h

        import tkinter.filedialog as fd
        save_path = fd.asksaveasfilename(
            title="Save Playlist",
            filetypes=[("Beat Saber Playlist", "*.bplist"), ("All files", "*.*")],
            defaultextension=".bplist",
            parent=parent or self,
        )
        if not save_path:
            return

        title = Path(save_path).stem

        image_data = ""
        for song in valid:
            if song.cover_path and song.cover_path.exists():
                try:
                    buf = io.BytesIO()
                    Image.open(song.cover_path).convert("RGB").save(buf, format="JPEG")
                    image_data = base64.b64encode(buf.getvalue()).decode("ascii")
                except Exception:
                    pass
                break

        playlist = {
            "playlistTitle": title,
            "playlistAuthor": "",
            "image": image_data,
            "customData": {},
            "songs": [
                {
                    "key": s.song_id,
                    "hash": hash_overrides.get(s.folder, s.song_hash),
                    "songName": s.display_name,
                }
                for s in valid
            ],
        }

        content = json.dumps(playlist, ensure_ascii=False, indent=2)
        target = Path(save_path)
        fd, tmp_str = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_str, target)
        except:
            Path(tmp_str).unlink(missing_ok=True)
            raise

        messagebox.showinfo(
            "Playlist Saved",
            f"Saved {len(valid)} songs to {target.name}",
        )

    # ── Playlist import (drag-and-drop + File menu) ───────────────────────────

    def _setup_playlist_dnd(self) -> None:
        for widget in (self, self.canvas):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind('<<Drop>>', self._on_playlist_drop)
            widget.dnd_bind('<<DropEnter>>', self._on_playlist_drop_enter)
            widget.dnd_bind('<<DropLeave>>', self._on_playlist_drop_leave)

    def _on_playlist_drop_enter(self, _event) -> None:
        self._drag_prev_status = self.status_bar.cget("text")
        self.status_bar.config(text="Drop .bplist file to open playlist…")

    def _on_playlist_drop_leave(self, _event) -> None:
        self.status_bar.config(text=getattr(self, "_drag_prev_status", ""))

    def _on_playlist_drop(self, event) -> None:
        self.status_bar.config(text=getattr(self, "_drag_prev_status", ""))
        path = self.tk.splitlist(event.data)[0]
        if Path(path).suffix.lower() not in {".bplist", ".json"}:
            return
        self._load_playlist_to_queue(path, anchor=self)

    def _open_playlist(self) -> None:
        import tkinter.filedialog as fd
        path = fd.askopenfilename(
            title="Open Playlist",
            filetypes=[("Beat Saber Playlist", "*.bplist"), ("All files", "*.*")],
        )
        if not path:
            return
        self._load_playlist_to_queue(path, anchor=self)

    def _load_playlist_from_path(self, path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            messagebox.showerror("Error", f"Could not read playlist:\n{e}")
            return

        entries = data.get("songs", [])
        if not entries:
            messagebox.showinfo("Empty Playlist", "The playlist contains no songs.")
            return

        img_b64 = data.get("image", "")
        if img_b64:
            self._playlist_art_b64 = img_b64
            self._playlist_art_locked = True
            self._playlist_art_first_song_key = None
            self._notify_playlist_art_window()
        else:
            self._playlist_art_locked = False

        hash_to_song = {s.song_hash.upper(): s for s in self.songs if s.song_hash}

        found: list[SongInfo] = []
        missing: list[dict] = []
        for entry in entries:
            h = (entry.get("hash") or "").upper()
            if h in hash_to_song:
                found.append(hash_to_song[h])
            else:
                missing.append(entry)

        if not missing:
            self._play_queue(found)
            title = data.get("playlistTitle", Path(path).stem)
            self.status_bar.config(text=f"Playlist '{title}': {len(found)} songs queued")
            return

        installable = [e for e in missing if e.get("key") or e.get("id")]
        uninstallable_count = len(missing) - len(installable)

        names = "\n".join(f"  • {e.get('songName', 'Unknown')}" for e in missing[:10])
        if len(missing) > 10:
            names += f"\n  … and {len(missing) - 10} more"

        if not installable:
            msg = (
                f"{len(missing)} song(s) are not installed and cannot be auto-installed "
                f"(no BeatSaver key):\n\n{names}"
            )
            if found:
                msg += f"\n\nQueue the {len(found)} available song(s) instead?"
                if messagebox.askyesno("Missing Songs", msg):
                    self._play_queue(found)
            else:
                messagebox.showerror("No Songs Available", msg)
            return

        msg = f"{len(missing)} song(s) are not installed:\n\n{names}\n\n"
        if uninstallable_count:
            msg += f"({uninstallable_count} cannot be auto-installed — no BeatSaver key.)\n\n"
        msg += (
            f"Install {len(installable)} song(s) via Mod Assistant and queue all "
            f"songs when done?\n\nSelecting 'No' will queue only the "
            f"{len(found)} already-installed song(s)."
        )

        if not messagebox.askyesno("Missing Songs", msg):
            if found:
                self._play_queue(found)
            return

        self._pending_playlist_entries = list(entries)

        # Preferred path: hand the whole playlist to Mod Assistant in one
        # shot via bsplaylist://. Mod Assistant downloads every missing
        # song in a single window. Falls back to the per-song beatsaver://
        # loop if the protocol isn't registered.
        if PlaylistInstaller.has_handler():
            expected_keys = [
                (e.get("key") or e.get("id") or "") for e in installable
            ]
            launched = self._playlist_installer.install(Path(path), expected_keys)
            if launched:
                self._pending_playlist_queue = []
                return

        self._pending_playlist_queue = installable[:]
        self._install_next_playlist_song()

    def _load_playlist_to_queue(self, path: str, anchor: tk.Misc | None = None) -> None:
        """Entry point for Queue-window DnD drops. Prompts when queue is non-empty."""
        if not self._queue:
            self._load_playlist_from_path(path)
            return
        choice = _ask_overwrite_or_append(self, anchor=anchor)
        if choice == "overwrite":
            self._load_playlist_from_path(path)
        elif choice == "append":
            self._append_playlist_from_path(path)

    def _append_playlist_from_path(self, path: str) -> None:
        """Append matched songs to the existing queue without changing playlist art."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            messagebox.showerror("Error", f"Could not read playlist:\n{e}")
            return

        entries = data.get("songs", [])
        if not entries:
            messagebox.showinfo("Empty Playlist", "The playlist contains no songs.")
            return

        hash_to_song = {s.song_hash.upper(): s for s in self.songs if s.song_hash}
        found: list[SongInfo] = []
        missing_count = 0
        for entry in entries:
            h = (entry.get("hash") or "").upper()
            if h in hash_to_song:
                song = hash_to_song[h]
                if song.audio_path:
                    found.append(song)
            else:
                missing_count += 1

        if not found:
            messagebox.showinfo("No Songs Available",
                                "No installed songs matched the playlist.")
            return

        self._queue.extend(found)
        self._notify_queue_window()

        title = data.get("playlistTitle", Path(path).stem)
        msg = f"Appended {len(found)} songs from '{title}'"
        if missing_count:
            msg += f"  •  {missing_count} not installed (skipped)"
        self.status_bar.config(text=msg)

    def _install_next_playlist_song(self) -> None:
        if not self._pending_playlist_queue:
            return
        entry = self._pending_playlist_queue.pop(0)
        song_id = entry.get("key") or entry.get("id")
        self._install_manager.trigger(song_id)

    def _install_playlist_from_url(self, url: str) -> None:
        """Download a remote .bplist and install via Mod Assistant."""
        self._pending_playlist_url = url
        self.status_bar.config(text="Downloading playlist…")

        def _download():
            try:
                filename = url.rstrip("/").split("/")[-1]
                if not filename.lower().endswith((".bplist", ".json")):
                    filename += ".bplist"
                fd, tmp_str = tempfile.mkstemp(suffix="_" + filename)
                os.close(fd)
                tmp_path = Path(tmp_str)
                urllib.request.urlretrieve(url, tmp_path)
                self.after(0, lambda: self._on_playlist_url_downloaded(tmp_path))
            except Exception as exc:
                self.after(0, lambda e=exc: self.status_bar.config(text=f"Download failed: {e}"))

        threading.Thread(target=_download, daemon=True).start()

    def _on_playlist_url_downloaded(self, tmp_path: Path) -> None:
        """Parse the downloaded .bplist and hand it to Mod Assistant."""
        try:
            with open(tmp_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self.status_bar.config(text=f"Could not parse playlist: {e}")
            tmp_path.unlink(missing_ok=True)
            return

        entries = data.get("songs", [])
        installable = [e for e in entries if e.get("key") or e.get("id")]
        expected_keys = [(e.get("key") or e.get("id") or "") for e in installable]

        self._pending_playlist_temp_path = tmp_path

        if not PlaylistInstaller.has_handler():
            self.status_bar.config(
                text="bsplaylist:// handler not found — install Mod Assistant."
            )
            tmp_path.unlink(missing_ok=True)
            self._pending_playlist_temp_path = None
            return

        launched = self._playlist_installer.install(tmp_path, expected_keys)
        if not launched:
            tmp_path.unlink(missing_ok=True)
            self._pending_playlist_temp_path = None

    def _on_playlist_install_complete(self, success: bool) -> None:
        """Called by PlaylistInstaller once Mod Assistant has finished
        (or timed out / been cancelled). Reload songs and queue everything
        the playlist references."""
        if not success:
            self.status_bar.config(
                text="Playlist install did not complete — queuing what's available."
            )
        if self._pending_playlist_temp_path:
            try:
                self._pending_playlist_temp_path.unlink(missing_ok=True)
            except Exception:
                pass
            self._pending_playlist_temp_path = None
        if self._pending_playlist_url:
            self._pending_playlist_url = None
            self.search_var.set("")
        # Trigger a song-library reload; _after_install_load then calls
        # _check_pending_playlist which will queue the songs.
        self._on_install_complete_reload()

    def _check_pending_playlist(self) -> None:
        # bsplaylist vs. per-song fallback contract:
        #   * bsplaylist path: `_install_playlist_from_path` initializes
        #     `_pending_playlist_queue = []` *after* launching Mod Assistant.
        #     The empty queue is load-bearing — it makes us fall through to the
        #     "everything is installed, queue the songs" branch below.
        #   * per-song fallback: `_pending_playlist_queue` is populated with
        #     `installable[:]` and drained one entry at a time by
        #     `_install_next_playlist_song()`.
        # If a future change forgets to reset `_pending_playlist_queue` between
        # playlists, the next install would resume from a stale list — keep the
        # reset at the bsplaylist branch.
        if self._pending_playlist_entries is None:
            return
        hash_to_song = {s.song_hash.upper(): s for s in self.songs if s.song_hash}
        if self._pending_playlist_queue:
            self._install_next_playlist_song()
            return
        queue = []
        for entry in self._pending_playlist_entries:
            h = (entry.get("hash") or "").upper()
            if h in hash_to_song:
                queue.append(hash_to_song[h])
        self._pending_playlist_entries = None
        if queue:
            self._play_queue(queue)
            self.status_bar.config(text=f"Playlist loaded: {len(queue)} songs queued")

    # ── Sub-window orchestration ──────────────────────────────────────────────

    def _open_queue_window(self):
        if self._queue_window and self._queue_window.winfo_exists():
            self._queue_window.deiconify()
            self._queue_window.lift()
            self._queue_window.focus_force()
            return
        self._queue_window = QueueWindow(self)

    def _notify_queue_window(self):
        if self._queue_window and self._queue_window.winfo_exists():
            self._queue_window.refresh()
        self._update_playlist_art_auto()

    def _notify_queue_library_reloaded(self):
        """Tell the queue window to drop its thumbnail/duration caches.

        Called from the post-install reload path so that a newly-installed song
        (which may replace an existing folder) shows the fresh cover art and
        duration in any open queue window.
        """
        if self._queue_window and self._queue_window.winfo_exists():
            try:
                self._queue_window.invalidate_caches()
                self._queue_window.refresh()
            except Exception:
                pass

    def _open_playlist_art_window(self):
        if self._playlist_art_window and self._playlist_art_window.winfo_exists():
            self._playlist_art_window.lift()
            self._playlist_art_window.focus_force()
            return
        self._playlist_art_window = PlaylistArtWindow(self)

    def _notify_playlist_art_window(self):
        if self._playlist_art_window and self._playlist_art_window.winfo_exists():
            self._playlist_art_window.refresh()

    def _update_playlist_art_auto(self):
        if self._playlist_art_locked:
            return
        first = self._queue[0] if self._queue else None
        key = str(first.folder) if first else None
        if key == self._playlist_art_first_song_key:
            return
        self._playlist_art_first_song_key = key
        if first and first.cover_path and first.cover_path.exists():
            try:
                buf = io.BytesIO()
                Image.open(first.cover_path).convert("RGB").save(buf, format="JPEG")
                self._playlist_art_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            except Exception:
                self._playlist_art_b64 = None
        else:
            self._playlist_art_b64 = None
        self._notify_playlist_art_window()
