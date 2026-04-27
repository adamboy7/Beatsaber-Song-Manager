"""
Beat Saber Custom Song Browser
Parses Steam library to locate Beat Saber, then lists all custom songs
with cover art and metadata. Click art or title to select a song.
"""

import os
import re
import json
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from PIL import Image, ImageTk
import datetime
import threading

# ─── Constants ────────────────────────────────────────────────────────────────

BEATSABER_APP_ID = "620980"
STEAM_RELATIVE_PATH = Path("steamapps/common/Beat Saber/Beat Saber_Data/CustomLevels")
DEFAULT_VDF_PATH   = Path(r"C:\Program Files (x86)\Steam\steamapps\libraryfolders.vdf")
THUMBNAIL_SIZE     = (80, 80)
WINDOW_TITLE       = "Beat Saber – Custom Song Browser"
BG_COLOR           = "#0d0d0d"
ACCENT_COLOR       = "#c724b1"       # Beat Saber magenta
TEXT_COLOR         = "#ffffff"
SUBTEXT_COLOR      = "#aaaaaa"
SELECTED_BG        = "#2a0033"
HOVER_BG           = "#1a001f"
ITEM_BG            = "#111111"
SEPARATOR_COLOR    = "#2a002e"
SCROLLBAR_BG       = "#1a001f"

# ─── VDF / path helpers ───────────────────────────────────────────────────────

def parse_vdf_library_paths(vdf_path: Path) -> list[Path]:
    """Return every Steam library root found in libraryfolders.vdf."""
    paths = []
    try:
        text = vdf_path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r'"path"\s+"([^"]+)"', text):
            p = Path(match.group(1).replace("\\\\", "\\"))
            if p.exists():
                paths.append(p)
    except FileNotFoundError:
        pass
    return paths


def find_beatsaber_custom_levels(vdf_path: Path = DEFAULT_VDF_PATH) -> Path | None:
    """Locate the CustomLevels folder by scanning Steam library folders."""
    library_roots = parse_vdf_library_paths(vdf_path)
    for root in library_roots:
        candidate = root / STEAM_RELATIVE_PATH
        if candidate.is_dir():
            return candidate
    return None


# ─── Song data ────────────────────────────────────────────────────────────────

class SongInfo:
    __slots__ = (
        "folder", "song_id", "display_name",
        "song_name", "sub_name", "author",
        "mapper", "bpm", "cover_path", "created_at",
        "diff_labels",
    )

    def __init__(self, folder: Path):
        self.folder = folder
        self.song_id = ""
        self.display_name = folder.name
        self.song_name = ""
        self.sub_name = ""
        self.author = ""
        self.mapper = ""
        self.bpm = 0.0
        self.cover_path: Path | None = None
        self.diff_labels: dict[int, str] = {}
        # Use st_birthtime (Windows/macOS) with st_ctime as fallback
        stat = folder.stat()
        self.created_at: float = getattr(stat, "st_birthtime", stat.st_ctime)
        self._parse()

    def _parse(self):
        # Detect community format: "abcd1 (Song Name - Mapper)"
        m = re.match(r'^([A-Za-z0-9]+)\s+\((.+)\)$', self.folder.name)
        if m:
            self.song_id = m.group(1)

        # Read Info.dat (case-insensitive search)
        info_file = None
        for name in ("Info.dat", "info.dat", "INFO.DAT"):
            candidate = self.folder / name
            if candidate.exists():
                info_file = candidate
                break

        if info_file:
            try:
                data = json.loads(info_file.read_text(encoding="utf-8", errors="replace"))
                self.song_name  = data.get("_songName", "")
                self.sub_name   = data.get("_songSubName", "")
                self.author     = data.get("_songAuthorName", "")
                self.mapper     = data.get("_levelAuthorName", "")
                self.bpm        = float(data.get("_beatsPerMinute", 0))
                cover_filename  = data.get("_coverImageFilename", "")
                if cover_filename:
                    cp = self.folder / cover_filename
                    if cp.exists():
                        self.cover_path = cp

                _DIFF_STR_TO_INT = {"Easy": 0, "Normal": 1, "Hard": 2, "Expert": 3, "ExpertPlus": 4}
                standard_labels: dict[int, str] = {}
                other_labels:    dict[int, str] = {}
                for bms in data.get("_difficultyBeatmapSets", []):
                    char = bms.get("_beatmapCharacteristicName", "")
                    for bm in bms.get("_difficultyBeatmaps", []):
                        diff_int = _DIFF_STR_TO_INT.get(bm.get("_difficulty", ""))
                        if diff_int is None:
                            continue
                        # V2 uses _customData, V3/V4 uses customData
                        custom = bm.get("_customData", bm.get("customData", {}))
                        label = custom.get("_difficultyLabel", custom.get("difficultyLabel", "")).strip()
                        if not label:
                            continue
                        if char == "Standard":
                            standard_labels[diff_int] = label
                        else:
                            other_labels.setdefault(diff_int, label)
                self.diff_labels = {**other_labels, **standard_labels}
            except Exception:
                pass

        # Build display name
        if self.song_name:
            self.display_name = self.song_name
            if self.sub_name:
                self.display_name += f" {self.sub_name}"
        else:
            self.display_name = self.folder.name

    @property
    def bpm_str(self) -> str:
        return f"{self.bpm:.0f} BPM" if self.bpm else ""

    @property
    def author_line(self) -> str:
        parts = []
        if self.author:
            parts.append(self.author)
        if self.mapper:
            parts.append(f"mapped by {self.mapper}")
        return "  •  ".join(parts)


def load_songs(custom_levels: Path) -> list[SongInfo]:
    songs = []
    for entry in custom_levels.iterdir():
        if entry.is_dir():
            songs.append(SongInfo(entry))
    # Newest folder first
    songs.sort(key=lambda s: s.created_at, reverse=True)
    return songs


# ─── PlayerData.dat ───────────────────────────────────────────────────────────

# Beat Saber difficulty int → label
DIFF_LABELS = {0: "Easy", 1: "Normal", 2: "Hard", 3: "Expert", 4: "ExpertPlus"}

# Beat Saber maxRank int → display string
RANK_LABELS = {0: "E", 1: "D", 2: "C", 3: "B", 4: "A", 5: "S", 6: "SS"}

# Colour per rank for display
RANK_COLORS = {
    "SS": "#FFD700", "S": "#FFD700",
    "A":  "#84e060", "B": "#60b4e0",
    "C":  "#e0c060", "D": "#e08060",
    "E":  "#888888", "DNF": "#555555",
}


def find_player_data() -> tuple["Path | None", str]:
    """Locate PlayerData.dat. Returns (path_or_None, debug_string)."""
    debug = []
    bs_relative = Path("Hyperbolic Magnetism") / "Beat Saber" / "PlayerData.dat"

    # Strategy 1: Registry — 'Local AppData' value tells us the real path;
    # LocalLow is always a sibling folder (Local -> LocalLow).
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
        )
        local_appdata, _ = winreg.QueryValueEx(key, "Local AppData")
        winreg.CloseKey(key)
        local_low = Path(local_appdata).parent / "LocalLow"
        debug.append(f"Registry->LocalLow:{local_low}")
        candidate = local_low / bs_relative
        if candidate.exists():
            return candidate, " | ".join(debug)
    except Exception as e:
        debug.append(f"registry_fail:{e}")

    # Strategy 2: USERPROFILE environment variable
    try:
        profile = os.environ.get("USERPROFILE", "")
        if profile:
            local_low = Path(profile) / "AppData" / "LocalLow"
            debug.append(f"USERPROFILE->LocalLow:{local_low}")
            candidate = local_low / bs_relative
            if candidate.exists():
                return candidate, " | ".join(debug)
    except Exception as e:
        debug.append(f"userprofile_fail:{e}")

    # Strategy 3: Path.home()
    local_low = Path.home() / "AppData" / "LocalLow"
    debug.append(f"home()->LocalLow:{local_low}")
    candidate = local_low / bs_relative
    if candidate.exists():
        return candidate, " | ".join(debug)

    return None, "NOT_FOUND | " + " | ".join(debug)


class DiffStat:
    """High score, rank string, and play count for one difficulty."""
    __slots__ = ("score", "rank", "plays")

    def __init__(self, score: int, rank: str, plays: int):
        self.score = score
        self.rank  = rank
        self.plays = plays


def load_favorites(player_data_path: Path) -> set[str]:
    """Return the set of favorited levelIds from PlayerData.dat."""
    try:
        raw = json.loads(player_data_path.read_text(encoding="utf-8", errors="replace"))
        players = raw.get("localPlayers", [])
        if not players:
            return set()
        return set(players[0].get("favoritesLevelIds", []))
    except Exception:
        return set()


def load_player_stats(player_data_path: Path) -> dict[str, dict[int, DiffStat]]:
    """Return {level_id: {difficulty_int: DiffStat}} for all entries."""
    stats: dict[str, dict[int, DiffStat]] = {}
    try:
        raw = json.loads(player_data_path.read_text(encoding="utf-8", errors="replace"))
        players = raw.get("localPlayers", [])
        if not players:
            return stats
        entries = players[0].get("levelsStatsData", [])
        for entry in entries:
            plays = entry.get("playCount", 0)
            if plays == 0:
                continue  # Beat Saber creates zero entries on scan; not actual play data

            level_id = entry.get("levelId", "")
            diff     = entry.get("difficulty", 0)
            score    = entry.get("highScore", 0)
            rank_int = entry.get("maxRank", 0)

            rank_str = RANK_LABELS.get(rank_int, "E") if score > 0 else "DNF"

            stat = DiffStat(score, rank_str, plays)
            stats.setdefault(level_id, {})[diff] = stat
    except Exception:
        pass
    return stats


def song_level_ids(song: "SongInfo") -> list[str]:
    """Generate the candidate levelId strings Beat Saber uses for this song.

    Beat Saber stores custom maps as:
      • "custom_level_{folder_name}"  — for community maps (short hex or SHA1)
      • levelId == folder name        — for built-in / OST maps
    """
    ids = [
        f"custom_level_{song.folder.name}",  # primary: matches the save exactly
        song.folder.name,                     # fallback for OST / built-in
    ]
    return ids


def get_song_stats(song: "SongInfo",
                   all_stats: dict[str, dict[int, DiffStat]]
                   ) -> dict[int, DiffStat] | None:
    """Find the stats dict for a song, trying all known levelId forms."""
    for lid in song_level_ids(song):
        if lid in all_stats:
            return all_stats[lid]
    return None


def format_diff_stats(diff_stats: dict[int, DiffStat],
                      custom_labels: dict[int, str] | None = None) -> tuple[str, str]:
    """Return (scores_line, plays_line) for display in the row."""
    # Only include difficulties that are actually tracked in the save file
    ordered = sorted(diff_stats.items())   # sort by difficulty int
    parts = []
    total_plays = 0
    for diff_int, stat in ordered:
        label = (custom_labels or {}).get(diff_int) or DIFF_LABELS.get(diff_int, f"D{diff_int}")
        # Only abbreviate standard names; custom labels are shown as-is
        short = {"Easy": "Easy", "Normal": "Norm", "Hard": "Hard",
                 "Expert": "Expert", "ExpertPlus": "E+"}.get(label, label)
        score_str = f"{stat.score:,}" if stat.score else "0"
        parts.append(f"{short}:{score_str} | {stat.rank}")
        total_plays += stat.plays

    scores_line = "  •  ".join(parts) if parts else ""
    plays_line  = f"Plays: {total_plays}" if total_plays else ""
    return scores_line, plays_line



# ─── UI ───────────────────────────────────────────────────────────────────────

class SongBrowser(tk.Tk):
    def __init__(self, custom_levels: Path):
        super().__init__()
        self.custom_levels = custom_levels
        self.songs: list[SongInfo] = []
        self.filtered: list[SongInfo] = []
        self.selected_index: int | None = None
        self._thumbnails: dict[int, ImageTk.PhotoImage] = {}   # keep refs alive
        self._placeholder: ImageTk.PhotoImage | None = None
        self._row_frames: list[tk.Frame] = []

        self.player_stats: dict = {}
        self.favorite_ids: set[str] = set()
        player_dat, pd_debug = find_player_data()
        if player_dat:
            self.player_stats = load_player_stats(player_dat)
            self.favorite_ids = load_favorites(player_dat)
            self.player_data_status = f"PlayerData: {len(self.player_stats)} entries  |  {player_dat.name}"
        else:
            self.player_data_status = f"PlayerData not found: {pd_debug}"

        self.title(WINDOW_TITLE)
        self.configure(bg=BG_COLOR)
        self.geometry("780x680")
        self.minsize(600, 400)

        self._build_ui()
        self._load_async()

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Header
        header = tk.Frame(self, bg=BG_COLOR)
        header.pack(fill="x", padx=16, pady=(14, 4))

        tk.Label(
            header, text="🎵  Custom Songs",
            font=("Segoe UI", 18, "bold"),
            bg=BG_COLOR, fg=ACCENT_COLOR
        ).pack(side="left")

        self.count_label = tk.Label(
            header, text="",
            font=("Segoe UI", 10),
            bg=BG_COLOR, fg=SUBTEXT_COLOR
        )
        self.count_label.pack(side="left", padx=10, pady=4)

        # Search bar
        search_frame = tk.Frame(self, bg=BG_COLOR)
        search_frame.pack(fill="x", padx=16, pady=(0, 8))

        tk.Label(search_frame, text="🔍", bg=BG_COLOR, fg=SUBTEXT_COLOR,
                 font=("Segoe UI", 11)).pack(side="left")

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self._on_search)

        search_entry = tk.Entry(
            search_frame,
            textvariable=self.search_var,
            font=("Segoe UI", 11),
            bg="#1e1e1e", fg=TEXT_COLOR,
            insertbackground=TEXT_COLOR,
            relief="flat",
            bd=6,
        )
        search_entry.pack(side="left", fill="x", expand=True, ipady=4)

        # Path label
        path_label = tk.Label(
            self,
            text=f"📂  {self.custom_levels}",
            font=("Segoe UI", 8),
            bg=BG_COLOR, fg="#555555",
            anchor="w",
        )
        path_label.pack(fill="x", padx=16, pady=(0, 6))

        # Scrollable song list
        container = tk.Frame(self, bg=BG_COLOR)
        container.pack(fill="both", expand=True, padx=16, pady=(0, 10))

        self.canvas = tk.Canvas(container, bg=BG_COLOR, highlightthickness=0)
        scrollbar = tk.Scrollbar(container, orient="vertical",
                                 command=self.canvas.yview,
                                 bg=SCROLLBAR_BG)
        self.canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.list_frame = tk.Frame(self.canvas, bg=BG_COLOR)
        self.canvas_window = self.canvas.create_window(
            (0, 0), window=self.list_frame, anchor="nw"
        )

        self.list_frame.bind("<Configure>", self._on_frame_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)

        # Status / selection bar
        self.status_bar = tk.Label(
            self,
            text="Loading songs…",
            font=("Segoe UI", 9),
            bg="#0a0a0a", fg=SUBTEXT_COLOR,
            anchor="w",
            pady=4,
        )
        self.status_bar.pack(fill="x", padx=16, pady=(0, 6))

    # ── Song loading ──────────────────────────────────────────────────────────

    def _load_async(self):
        def worker():
            songs = load_songs(self.custom_levels)
            self.after(0, lambda: self._on_loaded(songs))

        threading.Thread(target=worker, daemon=True).start()

    def _on_loaded(self, songs: list[SongInfo]):
        self.songs = songs
        self.filtered = songs[:]
        self.count_label.config(text=f"({len(songs)} songs)")
        self.status_bar.config(text=f"{len(songs)} songs found  •  {self.player_data_status}")
        self._render_list()

    # ── List rendering ────────────────────────────────────────────────────────

    def _make_placeholder(self) -> ImageTk.PhotoImage:
        if self._placeholder is None:
            img = Image.new("RGB", THUMBNAIL_SIZE, color="#2a0033")
            # Draw a simple music note shape using pixel art
            px = img.load()
            cx, cy = THUMBNAIL_SIZE[0] // 2, THUMBNAIL_SIZE[1] // 2
            for dy in range(-15, 16):
                for dx in range(-2, 3):
                    x, y = cx + dx, cy + dy - 5
                    if 0 <= x < THUMBNAIL_SIZE[0] and 0 <= y < THUMBNAIL_SIZE[1]:
                        px[x, y] = (199, 36, 177)
            for dx in range(0, 14):
                for dy in range(-2, 3):
                    x, y = cx + dx, cy - 20 + dy
                    if 0 <= x < THUMBNAIL_SIZE[0] and 0 <= y < THUMBNAIL_SIZE[1]:
                        px[x, y] = (199, 36, 177)
            self._placeholder = ImageTk.PhotoImage(img)
        return self._placeholder

    def _load_thumbnail(self, song: SongInfo, idx: int) -> ImageTk.PhotoImage:
        if idx in self._thumbnails:
            return self._thumbnails[idx]
        try:
            if song.cover_path:
                img = Image.open(song.cover_path).convert("RGB")
                img = img.resize(THUMBNAIL_SIZE, Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                self._thumbnails[idx] = photo
                return photo
        except Exception:
            pass
        return self._make_placeholder()

    def _render_list(self):
        # Clear existing rows
        for w in self.list_frame.winfo_children():
            w.destroy()
        self._row_frames.clear()

        for idx, song in enumerate(self.filtered):
            self._build_row(idx, song)

        self._update_scroll()

    def _is_favorite(self, song: SongInfo) -> bool:
        return any(lid in self.favorite_ids for lid in song_level_ids(song))

    def _build_row(self, idx: int, song: SongInfo):
        # Row container
        row = tk.Frame(self.list_frame, bg=ITEM_BG, cursor="hand2")
        row.pack(fill="x", pady=1)
        self._row_frames.append(row)

        # Thumbnail (loaded lazily)
        thumb_img = self._load_thumbnail(song, self.filtered.index(song))
        thumb_lbl = tk.Label(row, image=thumb_img, bg=ITEM_BG, cursor="hand2")
        thumb_lbl.image = thumb_img   # keep ref
        thumb_lbl.pack(side="left", padx=8, pady=6)

        # Text block
        text_frame = tk.Frame(row, bg=ITEM_BG)
        text_frame.pack(side="left", fill="both", expand=True, padx=4, pady=6)

        title_lbl = tk.Label(
            text_frame,
            text=song.display_name,
            font=("Segoe UI", 11, "bold"),
            bg=ITEM_BG, fg=TEXT_COLOR,
            anchor="w", cursor="hand2",
        )
        title_lbl.pack(fill="x")

        if song.author_line:
            author_lbl = tk.Label(
                text_frame,
                text=song.author_line,
                font=("Segoe UI", 9),
                bg=ITEM_BG, fg=SUBTEXT_COLOR,
                anchor="w",
            )
            author_lbl.pack(fill="x")

        meta_parts = []
        if song.bpm_str:
            meta_parts.append(song.bpm_str)
        if song.song_id:
            meta_parts.append(f"ID: {song.song_id}")
        added_str = datetime.datetime.fromtimestamp(song.created_at).strftime("Added %b %d, %Y")
        meta_parts.append(added_str)

        if meta_parts:
            meta_lbl = tk.Label(
                text_frame,
                text="  •  ".join(meta_parts),
                font=("Segoe UI", 8),
                bg=ITEM_BG, fg="#666666",
                anchor="w",
            )
            meta_lbl.pack(fill="x")

        # Player stats lines
        diff_stats = get_song_stats(song, self.player_stats)
        is_fav = self._is_favorite(song)
        if diff_stats:
            scores_line, plays_line = format_diff_stats(diff_stats, song.diff_labels)
            if scores_line:
                scores_lbl = tk.Label(
                    text_frame,
                    text=scores_line,
                    font=("Courier New", 8),
                    bg=ITEM_BG, fg="#c724b1",
                    anchor="w",
                )
                scores_lbl.pack(fill="x")
            if plays_line:
                display_plays = ("★ " + plays_line) if is_fav else plays_line
                plays_lbl = tk.Label(
                    text_frame,
                    text=display_plays,
                    font=("Segoe UI", 8),
                    bg=ITEM_BG, fg="#FFD700" if is_fav else "#888888",
                    anchor="w",
                )
                plays_lbl.pack(fill="x")
        else:
            tk.Label(
                text_frame,
                text="Easy: 0 | DNF",
                font=("Courier New", 8),
                bg=ITEM_BG, fg="#555555",
                anchor="w",
            ).pack(fill="x")
            tk.Label(
                text_frame,
                text=("★ Plays: 0") if is_fav else "Plays: 0",
                font=("Segoe UI", 8),
                bg=ITEM_BG, fg="#FFD700" if is_fav else "#555555",
                anchor="w",
            ).pack(fill="x")

        # Separator
        sep = tk.Frame(self.list_frame, bg=SEPARATOR_COLOR, height=1)
        sep.pack(fill="x")

        # Bind click / hover to all widgets in the row
        widgets = [row, thumb_lbl, text_frame, title_lbl]
        for child in text_frame.winfo_children():
            widgets.append(child)

        for w in widgets:
            w.bind("<Button-1>",    lambda e, i=idx: self._select(i))
            w.bind("<Enter>",       lambda e, r=row, s=sep: self._hover(r, s, True))
            w.bind("<Leave>",       lambda e, r=row, s=sep: self._hover(r, s, False))
            w.bind("<MouseWheel>",  self._on_mousewheel)

    def _hover(self, row: tk.Frame, sep: tk.Frame, entering: bool):
        if self._row_is_selected(row):
            return
        bg = HOVER_BG if entering else ITEM_BG
        self._recolor_row(row, bg)

    def _row_is_selected(self, row: tk.Frame) -> bool:
        return row.cget("bg") == SELECTED_BG

    def _recolor_row(self, row: tk.Frame, bg: str):
        row.configure(bg=bg)
        for child in row.winfo_children():
            try:
                child.configure(bg=bg)
            except Exception:
                pass
            for grandchild in child.winfo_children():
                try:
                    grandchild.configure(bg=bg)
                except Exception:
                    pass

    def _select(self, idx: int):
        # Deselect previous
        if self.selected_index is not None and self.selected_index < len(self._row_frames):
            self._recolor_row(self._row_frames[self.selected_index], ITEM_BG)

        self.selected_index = idx
        self._recolor_row(self._row_frames[idx], SELECTED_BG)

        song = self.filtered[idx]
        self.status_bar.config(
            text=f"Selected: {song.display_name}"
                 + (f"  •  {song.author}" if song.author else "")
                 + (f"  •  {song.bpm_str}" if song.bpm_str else "")
        )

    # ── Search ────────────────────────────────────────────────────────────────

    def _on_search(self, *_):
        query = self.search_var.get().lower().strip()
        if not query:
            self.filtered = self.songs[:]
        else:
            self.filtered = [
                s for s in self.songs
                if query in s.display_name.lower()
                or query in s.author.lower()
                or query in s.mapper.lower()
                or query in s.song_id.lower()
            ]
        self.selected_index = None
        self._thumbnails.clear()   # free memory; will reload on render
        self._render_list()
        self.status_bar.config(text=f"{len(self.filtered)} songs shown")

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


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    # Try to find custom levels automatically
    custom_levels = find_beatsaber_custom_levels()

    if custom_levels is None:
        # Fallback: ask user
        import tkinter.filedialog as fd
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(
            "Beat Saber not found",
            "Could not locate Beat Saber automatically.\n"
            "Please select your CustomLevels folder manually.",
        )
        path_str = fd.askdirectory(title="Select CustomLevels folder")
        root.destroy()
        if not path_str:
            return
        custom_levels = Path(path_str)

    app = SongBrowser(custom_levels)
    app.mainloop()


if __name__ == "__main__":
    main()
