"""
Pagination, search, install handling, and scroll helpers for
SongBrowser.

Includes:
  • Pagination controls (prev/next, jump-to-page dialog,
    page-size dialog, status label updater).
  • Search box behavior — both substring filtering and
    BeatSaver one-click / map-URL detection that surfaces an
    install row.
  • Install completion hook that reloads the song library and
    re-runs the search.
  • Canvas scroll/configure callbacks and the "scroll to
    currently selected row" helper used by QueueWindow's "View
    Song" action.
"""

from __future__ import annotations

import random
import re
import threading
import tkinter as tk

from libraries.constants import (
    BG_COLOR, ACCENT_COLOR, TEXT_COLOR, SUBTEXT_COLOR,
    SELECTED_BG, HOVER_BG, SEPARATOR_COLOR,
)
from libraries.player_data import get_song_stats, song_level_ids
from libraries.song_data import SongInfo, load_songs, load_song_hashes


_TAG_RE = re.compile(r'\{(\w+)\}:(\S+)', re.IGNORECASE)

_DIFF_NAME_TO_INT = {
    "easy": 0, "normal": 1, "hard": 2, "expert": 3, "expertplus": 4,
    "0": 0, "1": 1, "2": 2, "3": 3, "4": 4,
}

_BPM_OP_RE = re.compile(r'^(<=|>=|<|>|==|=)?(\d+(?:\.\d+)?)$')

_KNOWN_TAGS = {"artist", "mapper", "title", "unplayed", "favorite", "fullcombo", "fc", "bpm", "difficulty", "custom"}
_YN_TAGS    = {"unplayed", "favorite", "fullcombo", "fc"}


def _has_invalid_tags(tags: list[tuple[str, str]]) -> bool:
    for tag, value in tags:
        if tag not in _KNOWN_TAGS:
            return True
        if tag in _YN_TAGS and value not in ("y", "n"):
            return True
        if tag == "bpm" and not _BPM_OP_RE.match(value):
            return True
        if tag == "difficulty" and not value:
            return True
    return False


def _parse_tags(query: str) -> tuple[list[tuple[str, str]], str]:
    tags: list[tuple[str, str]] = []
    plain = _TAG_RE.sub(
        lambda m: (tags.append((m.group(1).lower(), m.group(2).lower())) or ""),
        query,
    )
    return tags, plain.strip()


def _song_matches_tags(
    song, tags: list[tuple[str, str]], player_stats: dict, favorite_ids: set
) -> bool:
    # Compute the (potentially expensive) per-song stats lookup at most once even
    # if the user filters by several stats-derived tags simultaneously.
    needs_stats = any(t in ("unplayed", "fullcombo", "fc") for t, _ in tags)
    stats = get_song_stats(song, player_stats) if needs_stats else None
    for tag, value in tags:
        if tag == "artist":
            if value not in song.author.lower():
                return False
        elif tag == "mapper":
            if value not in song.mapper.lower():
                return False
        elif tag == "title":
            if value not in song.display_name.lower():
                return False
        elif tag == "unplayed":
            total_plays = sum(d.plays for d in stats.values()) if stats else 0
            is_unplayed = total_plays == 0
            if value == "y" and not is_unplayed:
                return False
            if value == "n" and is_unplayed:
                return False
        elif tag == "favorite":
            is_fav = any(lid in favorite_ids for lid in song_level_ids(song))
            if value == "y" and not is_fav:
                return False
            if value == "n" and is_fav:
                return False
        elif tag in ("fullcombo", "fc"):
            is_fc = any(d.full_combo for d in stats.values()) if stats else False
            if value == "y" and not is_fc:
                return False
            if value == "n" and is_fc:
                return False
        elif tag == "bpm":
            m = _BPM_OP_RE.match(value)
            if m:
                op, num = m.group(1) or "==", float(m.group(2))
                bpm = song.bpm
                passes = (
                    bpm <= num if op == "<=" else
                    bpm >= num if op == ">=" else
                    bpm <  num if op == "<"  else
                    bpm >  num if op == ">"  else
                    bpm == num
                )
                if not passes:
                    return False
            else:
                return False
        elif tag == "difficulty":
            diff_int = _DIFF_NAME_TO_INT.get(value)
            if diff_int is not None:
                if diff_int not in song.difficulties:
                    return False
            else:
                if not any(value == lbl.lower() for lbl in song.diff_labels.values()):
                    return False
        elif tag == "custom":
            if not any(value == t.lower() for t in song.custom_tags):
                return False
    return True


def filter_songs(
    songs: list, query: str, player_stats: dict, favorite_ids: set
) -> list:
    """Filter songs using the same search/tag logic as the GUI search box."""
    tags, plain = _parse_tags(query)
    plain_lower = plain.lower()
    return [
        s for s in songs
        if (
            (not plain_lower or (
                plain_lower in s.display_name.lower()
                or plain_lower in s.author.lower()
                or plain_lower in s.mapper.lower()
                or plain_lower in s.song_id.lower()
            ))
            and (not tags or _song_matches_tags(s, tags, player_stats, favorite_ids))
        )
    ]


def pick_random_songs(filtered: list | None, unfiltered: list, n: int) -> list:
    """Pick n songs prioritising filtered results, then supplementing from unfiltered.

    Priority order (no repeats until pool is exhausted):
    1. Random sample from filtered (up to n)
    2. Random sample from unfiltered songs not already picked
    3. Random choices (with repeats) from unfiltered only when even step 2 is exhausted
    If filtered is None or empty, treats unfiltered as the sole pool.
    """
    pool = filtered if filtered else unfiltered
    if n <= len(pool):
        return random.sample(pool, n)
    picks = random.sample(pool, len(pool))
    remaining = n - len(picks)
    if pool is not unfiltered:
        picked_set = {s.folder for s in picks}
        supplement = [s for s in unfiltered if s.folder not in picked_set]
        if remaining <= len(supplement):
            picks += random.sample(supplement, remaining)
            return picks
        picks += supplement
        remaining -= len(supplement)
    if remaining:
        picks += random.choices(unfiltered, k=remaining)
    return picks


class BrowserPaginationMixin:
    """Pagination, search, install completion, and scroll helpers."""

    # ── Pagination ────────────────────────────────────────────────────────────

    def _change_page_size(self):
        dlg = tk.Toplevel(self, bg=BG_COLOR)
        dlg.title("Results per page")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        tk.Label(
            dlg, text="Results per page:",
            bg=BG_COLOR, fg=TEXT_COLOR, font=("Segoe UI", 10),
        ).pack(padx=20, pady=(16, 6))

        entry = tk.Entry(
            dlg, font=("Segoe UI", 10), width=10,
            bg="#1e1e1e", fg=TEXT_COLOR, insertbackground=TEXT_COLOR,
            relief="flat", bd=4, justify="center",
        )
        entry.insert(0, str(self.page_size))
        entry.select_range(0, "end")
        entry.pack(padx=20, pady=(0, 12))
        entry.focus_set()

        btn_frame = tk.Frame(dlg, bg=BG_COLOR)
        btn_frame.pack(padx=20, pady=(0, 16))

        result: list[int | None] = [None]

        def _flash_invalid():
            # Brief red-tint to tell the user the entry wasn't a positive integer.
            try:
                entry.config(bg="#5a1f1f")
                entry.after(600, lambda: entry.config(bg="#1e1e1e"))
            except tk.TclError:
                pass

        def _confirm():
            text = entry.get().strip()
            try:
                val = int(text)
                if val < 1:
                    raise ValueError
                result[0] = val
                dlg.destroy()
            except ValueError:
                _flash_invalid()
                entry.focus_set()
                entry.select_range(0, "end")

        def _cancel():
            dlg.destroy()

        tk.Button(
            btn_frame, text="OK", width=8,
            bg="#1e1e1e", fg=TEXT_COLOR,
            activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR,
            relief="flat", bd=4, command=_confirm,
        ).pack(side="left", padx=(0, 8))

        tk.Button(
            btn_frame, text="Cancel", width=8,
            bg="#1e1e1e", fg=TEXT_COLOR,
            activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR,
            relief="flat", bd=4, command=_cancel,
        ).pack(side="left")

        entry.bind("<Return>", lambda _: _confirm())
        entry.bind("<Escape>", lambda _: _cancel())

        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{x}+{y}")

        self.wait_window(dlg)

        if result[0] is not None and result[0] != self.page_size:
            self.page_size = result[0]
            self.page = 0
            self._render_list()

    def _prev_page(self):
        if self.page > 0:
            self.page -= 1
            self._render_list()

    def _next_page(self):
        total_pages = max(1, (len(self.filtered) + self.page_size - 1) // self.page_size)
        if self.page < total_pages - 1:
            self.page += 1
            self._render_list()

    def _jump_to_page(self):
        total_pages = max(1, (len(self.filtered) + self.page_size - 1) // self.page_size)
        if total_pages <= 1:
            return

        dlg = tk.Toplevel(self, bg=BG_COLOR)
        dlg.title("Go to page")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        tk.Label(
            dlg, text=f"Enter page number (1–{total_pages}):",
            bg=BG_COLOR, fg=TEXT_COLOR, font=("Segoe UI", 10),
        ).pack(padx=20, pady=(16, 6))

        entry = tk.Entry(
            dlg, font=("Segoe UI", 10), width=10,
            bg="#1e1e1e", fg=TEXT_COLOR, insertbackground=TEXT_COLOR,
            relief="flat", bd=4, justify="center",
        )
        entry.insert(0, str(self.page + 1))
        entry.select_range(0, "end")
        entry.pack(padx=20, pady=(0, 12))
        entry.focus_set()

        btn_frame = tk.Frame(dlg, bg=BG_COLOR)
        btn_frame.pack(padx=20, pady=(0, 16))

        result: list[int | None] = [None]

        def _confirm():
            try:
                result[0] = int(entry.get())
            except ValueError:
                pass
            dlg.destroy()

        def _cancel():
            dlg.destroy()

        ok_btn = tk.Button(
            btn_frame, text="OK", width=8,
            bg="#1e1e1e", fg=TEXT_COLOR,
            activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR,
            relief="flat", bd=4, command=_confirm,
        )
        ok_btn.pack(side="left", padx=(0, 8))

        tk.Button(
            btn_frame, text="Cancel", width=8,
            bg="#1e1e1e", fg=TEXT_COLOR,
            activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR,
            relief="flat", bd=4, command=_cancel,
        ).pack(side="left")

        entry.bind("<Return>", lambda _: _confirm())
        entry.bind("<Escape>", lambda _: _cancel())

        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{x}+{y}")

        self.wait_window(dlg)

        if result[0] is not None:
            self.page = max(0, min(result[0] - 1, total_pages - 1))
            self._render_list()

    def _update_pagination_controls(self):
        total = len(self.filtered)
        total_pages = max(1, (total + self.page_size - 1) // self.page_size)
        self._prev_btn.config(state="normal" if self.page > 0 else "disabled")
        self._next_btn.config(state="normal" if self.page < total_pages - 1 else "disabled")
        if total == 0:
            self._page_label.config(text="")
        elif total_pages <= 1:
            self._page_label.config(text="")
        else:
            start = self.page * self.page_size + 1
            end = min(start + self.page_size - 1, total)
            self._page_label.config(
                text=f"Page {self.page + 1} of {total_pages}  •  {start}–{end} of {total}"
            )

    # ── Search ────────────────────────────────────────────────────────────────

    def _extract_song_id(self, query: str) -> str | None:
        """Return the BeatSaver song ID if query is a one-click or map URL, else None.

        BeatSaver keys are short hex strings (currently 1–6 chars in the wild);
        we bound the capture so a copy/paste accident like
        `https://beatsaver.com/maps/abc/comments` doesn't silently strip the
        trailing path and offer to install `abc`.
        """
        q = query.strip()
        m = re.match(r'^beatsaver://([A-Za-z0-9]{1,8})(?:[/?#]|$)', q, re.IGNORECASE)
        if m:
            return m.group(1).lower()
        m = re.match(r'^https?://beatsaver\.com/maps/([A-Za-z0-9]{1,8})(?:[/?#]|$)', q, re.IGNORECASE)
        if m:
            return m.group(1).lower()
        return None

    def _extract_playlist_url(self, query: str) -> str | None:
        """Return the inner HTTP URL if query is a bsplaylist:// one-click URL, else None."""
        m = re.match(r'^bsplaylist://playlist/(https?://\S+)', query.strip(), re.IGNORECASE)
        return m.group(1) if m else None

    def _on_search(self, *_):
        query = self.search_var.get().strip()

        song_id = self._extract_song_id(query)
        if song_id:
            installed = [s for s in self.songs if s.song_id.lower() == song_id]
            if installed:
                self.filtered = self._apply_view_filters(installed)
                self._pending_install_id = None
                self.status_bar.config(text=f"Song {song_id} is already installed.")
            else:
                self.filtered = []
                self._pending_install_id = song_id
                self.status_bar.config(
                    text=f"Song {song_id} not installed — press Enter or click to install via Mod Assistant."
                )
            self.selected_indices = {
                i for i, s in enumerate(self.filtered)
                if str(s.folder) in self._selected_folders
            }
            self.selected_index = max(self.selected_indices) if self.selected_indices else None
            # Thumbnail cache is keyed by folder path; the search result is a subset
            # of the library, so the existing cache is still valid. Don't clear it.
            self.page = 0
            self._render_list()
            self._update_search_icon_color()
            return

        playlist_url = self._extract_playlist_url(query)
        if playlist_url:
            self._pending_playlist_url = playlist_url
            self._pending_install_id = None
            self.filtered = []
            self.page = 0
            self._render_list()
            self.status_bar.config(
                text="Playlist URL detected — press Enter or click to install via Mod Assistant."
            )
            self._update_search_icon_color()
            return

        self._pending_playlist_url = None
        self._pending_install_id = None
        self._install_manager.cancel()
        raw_query = self.search_var.get().strip()
        tags, _ = _parse_tags(raw_query)

        if not raw_query:
            self.filtered = self.songs[:]
        else:
            self.filtered = filter_songs(self.songs, raw_query, self.player_stats, self.favorite_ids)
        self.filtered = self._apply_view_filters(self.filtered)
        self.selected_indices = {
            i for i, s in enumerate(self.filtered)
            if str(s.folder) in self._selected_folders
        }
        self.selected_index = max(self.selected_indices) if self.selected_indices else None
        # Thumbnail cache is keyed by folder path; rows shown after the filter are
        # always a subset of cached entries, so clearing on every keystroke is
        # over-defensive and causes search lag on slow disks.
        self.page = 0
        self._render_list()
        if tags:
            tag_summary = "  •  ".join(f"{{{t}}}:{v}" for t, v in tags)
            self.status_bar.config(text=f"{len(self.filtered)} songs shown  •  {tag_summary}")
        else:
            self.status_bar.config(text=f"{len(self.filtered)} songs shown")
        self._update_search_icon_color()

    def _on_search_enter(self, *_):
        if self._pending_playlist_url:
            self._install_playlist_from_url(self._pending_playlist_url)
        elif self._pending_install_id:
            self._trigger_install(self._pending_install_id)

    def _update_search_icon_color(self) -> None:
        from libraries.constants import SUBTEXT_COLOR
        query = self.search_var.get().strip()
        tags, _ = _parse_tags(query)
        if _has_invalid_tags(tags):
            color = "#ff5555"
        elif self._favorites_only or any(t == "favorite" and v == "y" for t, v in tags):
            color = "#FFD700"
        else:
            color = SUBTEXT_COLOR
        self.search_icon_label.config(fg=color)

    def _trigger_install(self, song_id: str):
        self._install_manager.trigger(song_id)

    # ── Install completion ────────────────────────────────────────────────────

    def _on_install_complete_reload(self):
        # Share the same generation counter as _load_async so a racing F5 + install
        # completion can't double-fire the destructive _on_loaded path.
        self._load_gen = getattr(self, "_load_gen", 0) + 1
        gen = self._load_gen

        def worker():
            songs = load_songs(self.custom_levels)
            hashes = load_song_hashes(self.custom_levels)
            for song in songs:
                song.song_hash = hashes.get(song.folder.name, "")
            self.after(0, lambda: self._maybe_after_install_load(gen, songs))

        threading.Thread(target=worker, daemon=True).start()

    def _maybe_after_install_load(self, gen: int, songs: list[SongInfo]):
        if gen != getattr(self, "_load_gen", 0):
            return  # superseded; drop stale result
        self._after_install_load(songs)

    def _after_install_load(self, songs: list[SongInfo]):
        self.songs = songs
        self.count_label.config(text=f"({len(songs)} songs)")
        # Queue thumbnail cache may now point at stale cover art for folders whose
        # contents changed during install — let the queue window invalidate it.
        try:
            self._notify_queue_library_reloaded()
        except AttributeError:
            pass
        self._on_search()  # re-applies search bar content; shows installed song and clears pending_install_id
        self._check_pending_playlist()

    def _build_install_row(self, song_id: str):
        row = tk.Frame(self.list_frame, bg=HOVER_BG, cursor="hand2")
        row.pack(fill="x", pady=1)

        icon_lbl = tk.Label(row, text="⬇", font=("Segoe UI", 20),
                            bg=HOVER_BG, fg=ACCENT_COLOR, width=4)
        icon_lbl.pack(side="left", padx=8, pady=6)

        text_frame = tk.Frame(row, bg=HOVER_BG)
        text_frame.pack(side="left", fill="both", expand=True, padx=4, pady=6)

        title_lbl = tk.Label(
            text_frame,
            text=f"Click to install {song_id}…",
            font=("Segoe UI", 11, "bold"),
            bg=HOVER_BG, fg=ACCENT_COLOR,
            anchor="w", cursor="hand2",
        )
        title_lbl.pack(fill="x")

        sub_lbl = tk.Label(
            text_frame,
            text="Opens one-click install via Mod Assistant  •  or press Enter",
            font=("Segoe UI", 9),
            bg=HOVER_BG, fg=SUBTEXT_COLOR,
            anchor="w",
        )
        sub_lbl.pack(fill="x")

        sep = tk.Frame(self.list_frame, bg=SEPARATOR_COLOR, height=1)
        sep.pack(fill="x")

        for w in [row, icon_lbl, text_frame, title_lbl, sub_lbl]:
            w.bind("<Button-1>",   lambda _, sid=song_id: self._trigger_install(sid))
            w.bind("<Enter>",      lambda _, r=row: self._recolor_row(r, SELECTED_BG))
            w.bind("<Leave>",      lambda _, r=row: self._recolor_row(r, HOVER_BG))
            w.bind("<MouseWheel>", self._on_mousewheel)

    # ── Scroll helpers ────────────────────────────────────────────────────────

    def _on_frame_configure(self, _):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfig(self.canvas_window, width=event.width)

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _update_scroll(self):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.canvas.yview_moveto(0)

    def _scroll_to_selected(self):
        """Scroll the song list so the currently selected row is centered in view.

        Safe to call when nothing is selected or when the selected song isn't on
        the currently rendered page — it just no-ops in those cases.
        """
        if self.selected_index is None:
            return
        page_start = self.page * self.page_size
        local_idx = self.selected_index - page_start
        if not (0 <= local_idx < len(self._row_frames)):
            return
        row = self._row_frames[local_idx]
        try:
            # Force geometry to settle so winfo_y / bbox return real values.
            self.list_frame.update_idletasks()
            self.canvas.update_idletasks()
            bbox = self.canvas.bbox("all")
            if not bbox:
                return
            total_height = bbox[3] - bbox[1]
            if total_height <= 0:
                return
            canvas_height = self.canvas.winfo_height()
            row_height = row.winfo_height()
            row_y = row.winfo_y()
            # Center the row in the viewport when possible.
            target_y = row_y - max(0, (canvas_height - row_height) // 2)
            target_y = max(0, min(target_y, total_height - canvas_height))
            fraction = target_y / total_height if total_height else 0.0
            self.canvas.yview_moveto(max(0.0, min(1.0, fraction)))
        except (tk.TclError, ZeroDivisionError):
            pass
