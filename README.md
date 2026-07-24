# Beat Saber Song Manager

A music browser, media player, playlist builder, and asset editor for Beat Saber custom maps. Browse your entire CustomLevels library, preview songs, manage favorites, shape playlists, and edit song files — all from one place.

---

## Setup

### Requirements

- **ffmpeg** — place `ffmpeg`/`ffmpeg.exe` and `ffprobe`/`ffprobe.exe` next to the application, or add them to your system PATH. [Download ffmpeg](https://ffmpeg.org/download.html) If it's missing when you convert audio, the app offers to download a prebuilt static build for you (from [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds), matching your platform — Windows or Linux), drops the binaries next to the app, and retries the conversion automatically — no manual download or PATH edit needed.
- **libmpv** — powers all audio and Cinema video playback. On **Windows**, place `libmpv-2.dll` next to the application or add it to PATH ([download](https://mpv.io/installation/), "libmpv" dev builds); if missing, the app offers to fetch it. On **Linux**, install it from your package manager: `sudo apt install libmpv2` (Debian/Ubuntu), `sudo dnf install mpv-libs` (Fedora), or `sudo pacman -S mpv` (Arch).
- Beat Saber installed via Steam (recommended, but not required — see below). On Linux, Beat Saber runs through Steam Play/Proton; the app locates your library and reads scores/favorites from the game's Proton prefix automatically.

### Linux

Native Linux support runs the same Python app. You'll also need Tk (`sudo apt install python3-tk` or your distro's equivalent) and the packages in `requirements.txt`. Run with `python3 Browser.py`, or build a standalone binary with `./build.sh` (uses `Browser.linux.spec`). See [Linux.md](Linux.md) for details on how library and score detection works under Proton.

### How the App Finds Your Files

On launch, the app locates your library automatically:

1. Asks Windows where AppData is → finds your Beat Saber score data (`PlayerData.dat`)
2. Asks Steam where the game is installed → finds your `CustomLevels` folder
3. If either step fails, it prompts you to point it to a `CustomLevels` folder manually

The app works even without Beat Saber installed. Point it at any folder of Beat Saber maps and it functions as a standalone music player and playlist manager.

### Installing Songs

Songs and playlists download directly from BeatSaver — no additional tools or setup required. As long as the app can find (or you point it at) a `CustomLevels` folder, you can install and curate maps even without Beat Saber installed.

---

## How to Read the UI

Three states, always consistent across every window:

- **White** — clickable
- **Grey** — not available right now
- **Highlighted** — something to pay attention to

---

## Main Window

### Opening a Playlist

Four ways, pick what's comfortable:

1. **File → Open** or **Ctrl+O** — standard file dialog
2. **Drag a `.bplist` file onto the window** — drop it anywhere
3. **Drag a `.bplist` onto the application file itself** — works before the window opens
4. **Command line** — pass a playlist path as an argument for scripted workflows

When a playlist is already loaded, you'll be asked to overwrite the current queue, append to it, or cancel.

### Finding a Song

**Plain text search** — searches across title, artist, mapper, and BeatSaver ID simultaneously. Forgiving and fast.

**Search tags** — filter by specific fields, play status, favorites, BPM ranges, and difficulty. Multiple tags combine in a single query. See [Search Tags](#search-tags) for the full reference.

**Visual browsing** — scroll through cover art and titles. Sometimes you know what you want when you see it.

**Chained searches** — selections persist across searches. Search for something, select a few songs, change your search, select more. Your picks accumulate. Export the whole selection as a playlist at any time, even if it came from three different searches.

### Playing a Song

- **Right-click → Play** — starts immediately if nothing else is playing
- **Right-click → Add to Queue** — adds to end of queue
- **Shift+right-click → Play** — jumps the queue and plays immediately
- **Command line** — passing a playlist as an argument starts playback of the first song automatically

### Navigating Playback

- **Hardware media keys** — respected system-wide while the app is running
- **Clickable player controls** — play/pause, next, previous, shuffle, loop
- **Queue window** — for full queue management, reordering, and editing

The media player bar can be hidden (Options → Show Media Player). If you prefer controlling playback through media keys and the Queue window alone, you can keep the main window lean or minimize it entirely.

### Song Actions

**Ctrl+Click** on a song's cover art or title opens its BeatSaver page in your browser.

Right-click a song for:

- **Play / Add to Queue**
- **Add to Favorites / Remove from Favorites**
- **Copy Link** — copies the BeatSaver URL to clipboard
- **Copy Name** — copies the song's display name
- **More from This Artist / More from This Mapper** — instantly filters the library to that artist or mapper
- **Download Video** — appears when a song ships a `cinema-video.json` whose video isn't downloaded yet. See [Cinema Video Support](#cinema-video-support).
- **Open Folder…** — opens the song's folder in Explorer
- **Delete** — disabled for favorited songs unless Shift is held

Right-click a multi-selection for:

- **Add to Queue**
- **Add to Favorites / Remove from Favorites**
- **Share Playlist** — exports your selection as a new `.bplist` file
- **Delete**

### Favorites

Favorited songs show a gold ★ and are protected from accidental deletion — the delete option won't appear without the Shift override. Right-click to add or remove favorites on single songs or multi-selections.

The **View** menu has quick toggles for **Favorites Only** and **Hide Favorites** that layer on top of any active search.

### Scores

Each song shows per-difficulty high scores, ranks, play counts, and full combo status from your Beat Saber save data. Full combos appear in colored text. Press **F5** to refresh after playing.

### Edit Menu (Shift+Right-Click)

Shift+right-click unlocks asset editing. All operations that modify a file create a **backup on first edit** — the original is always recoverable. Restore from the same menu.

- **Replace Art** — file picker for common image formats. Resized to match original dimensions. Reflects immediately in the UI.
- **Replace Audio** — file picker for common audio formats including Beat Saber's native `.ogg`/`.egg`. Non-OGG files are converted automatically.
- **Edit Info** — shown in red with a warning. Editing metadata changes the song's SHA1 hash, which breaks its identity on BeatSaver (install links, playlist matching). Fine for personal use; avoid if you plan to share the map.
- **Custom Tags…** — add or remove personal tags on a song (or a multi-selection). Tags are searchable via `{custom}:tagname`.
- **Clear Score** — removes score data for this song only. All other high scores are preserved.
- **Restore from Backup** — reverts to the backup created at first edit.

### Search Tags

All tags use `{tag}:value` syntax and are case-insensitive. Multiple tags can be combined in one query (space-separated). Plain text without a tag searches title, artist, mapper, and BeatSaver ID simultaneously.

| Tag | Values | Description |
|---|---|---|
| `{title}:TEXT` | any text | Filter by song title (substring match) |
| `{artist}:TEXT` | any text | Filter by artist (substring match) |
| `{mapper}:TEXT` | any text | Filter by mapper name (substring match) |
| `{unplayed}:y` / `:n` | `y` or `n` | Only unplayed / only played songs |
| `{favorite}:y` / `:n` | `y` or `n` | Only favorited / only non-favorited songs |
| `{fullcombo}:y` / `:n` | `y` or `n` | Only songs with / without a full combo |
| `{fc}:y` / `:n` | `y` or `n` | Alias for `{fullcombo}` |
| `{bpm}:OP N` | `<=`, `>=`, `<`, `>`, `=` + number | Filter by BPM — combine two for a range |
| `{difficulty}:NAME` | `easy`, `normal`, `hard`, `expert`, `expertplus` or `0`–`4` | Only songs that include this difficulty |
| `{custom}:TAG` | any text | Only songs with this custom tag (exact match, case-insensitive) |
| `{chroma}:y` / `:n` | `y` or `n` | Only songs that **require** Chroma / that don't |
| `{noodle}:y` / `:n` | `y` or `n` | Only songs that **require** Noodle Extensions / that don't |
| `{extensions}:y` / `:n` | `y` or `n` | Only songs that **require** Mapping Extensions / that don't |
| `{cinema}:y` / `:n` | `y` or `n` | Only songs that suggest/require Cinema or ship a `cinema-video.json` / that don't |

**Examples**

```
{mapper}:psi {unplayed}:y
```
Unplayed songs mapped by Psi.

```
{artist}:camellia {favorite}:y
```
Favorited Camellia songs.

```
{bpm}:>=150 {bpm}:<=200
```
Songs between 150 and 200 BPM.

```
{difficulty}:expertplus {fullcombo}:n
```
Expert+ songs without a full combo.

```
{difficulty}:4 {favorite}:y
```
Favorited Expert+ songs (numeric shorthand for difficulty).

```
{noodle}:n {chroma}:n {extensions}:n
```
Songs that don't require any mods beyond the base game.

### Installing from the Search Bar

The search bar doubles as an install target. Paste any of the following and an install row appears at the top of the list — press Enter or click it to proceed.

**Single songs**

- A BeatSaver map URL — `https://beatsaver.com/maps/ID`
- A one-click link — `beatsaver://ID`

The song downloads directly from BeatSaver, and the library reloads automatically when it finishes.

**Playlists**

- A direct `.bplist` URL — `https://example.com/playlist.bplist`
- A one-click playlist link — `bsplaylist://playlist/https://…`

The playlist file is downloaded, then every missing song is fetched from BeatSaver one after another, with live progress. The library reloads when it's done.

### Keyboard Shortcuts — Main Window

| Shortcut | Action |
|---|---|
| Ctrl+O | Open playlist |
| Ctrl+A | Select all visible |
| Escape | Deselect all |
| Ctrl+Click | Single select toggle (on row); open BeatSaver page (on cover art or title) |
| Shift+Click | Range select |
| Shift+Right-Click | Open edit menu |
| Space | Play / Pause (search bar must be unfocused) |
| F5 | Refresh library |
| Delete / Backspace | Delete selected |
| Enter | Confirm pending install |

---

## Queue Window

The Queue window is a self-contained media player workflow. Open a playlist, shape it, save it — the main window can be minimized or ignored entirely. Media keys work regardless of which window has focus.

### Playback Controls

Clickable buttons for play/pause, shuffle, loop, next, and previous. The **Queue button** opens a menu to clear the queue (with a confirmation prompt). Stop is in the menus.

**Shuffle Order** — different than the shuffle button. If you save after shuffling, the saved order is the shuffled order.

### Reordering

- **Drag and drop** rows to reorder
- **Menus** for Move to Top / Move to Bottom
- **Cut/Copy/Paste support** ctrl+ X/C/V

### Replacing Songs

Select one or more songs in the queue, then use Replace. A dialog appears with optional tag filters — press OK with defaults for a random pick from your whole library.

The system always tries to pick songs not already in the queue. If filtered picks run dry, it falls back to unfiltered picks, then allows repeats if the queue is larger than your library.

**Single song selected:** you can increase the count above 1 to insert additional songs at that position, keeping the rest of the queue in order. Useful for mid-queue inserts.

**Multiple songs selected:** the count is locked to match your selection — one replacement per slot. Replaces every song 1:1 in place.

The song being replaced is excluded from its own replacement pick, but may appear again in later replacements. If a song keeps showing up and you don't want it, refine your tag filter or remove it from your library in the main window.

### Cut, Copy, and Paste

The queue has an internal clipboard — your system clipboard is unaffected.

- **Ctrl+C** — copy selected songs to clipboard; clears any pending cut
- **Ctrl+X** — same as copy, but marks songs with a dark-red tint and leaves them in place until paste
- **Ctrl+V** — if one song is selected, inserts the clipboard after it; if multiple or none are selected, appends to end; no-op if clipboard is empty

If the currently playing song is marked for cut and you paste, playback stops and resumes from the first non-cut song in the original queue order. Closing the Queue window clears cut markers but keeps the clipboard — paste still works on reopen.

### Saving

**Ctrl+S** saves the current queue as a `.bplist` file. A warning appears if the queue is empty. Right-click → Save Queue also works.

Saved playlists are useful in three ways:
- Reimport them into the app as a saved session
- Reopen in the app to install any songs that aren't downloaded yet
- Drop into Beat Saber's playlist folder to use in-game

### Drag and Drop

Drag a `.bplist` onto the Queue window to open it — same overwrite/append/cancel dialog as the main window.

### View Song

The View Song button brings the selected song into focus in the main window, useful when you want full details (scores, mapper, difficulty info) on a queue item.

### Keyboard Shortcuts — Queue Window

| Shortcut | Action |
|---|---|
| Ctrl+O | Open playlist |
| Ctrl+S | Save queue as playlist |
| Ctrl+A | Select all |
| Escape | Deselect all |
| Ctrl+C | Copy to internal clipboard |
| Ctrl+X | Cut (dark-red tint until paste) |
| Ctrl+V | Paste after selection / append to end |
| Delete / Backspace | Remove selected from queue |

---

## Playlist Art Window

Access via **View → Playlist Art**. Only relevant when you're distributing a playlist to others — if you're just saving your queue for personal use, you can ignore this entirely.

- **New playlist** — cover art defaults to the first song's image automatically
- **Opened playlist** — existing art is imported; you can export it if you want it for other purposes
- **Drag an image onto the window** — replaces the current art
- **Right-click** — replace or export options
- **Clear** — removes custom art and resets to inheriting the first song's image

---

## Visualizer Window

Access via **View → Visualizer**. Shows a real-time frequency-bar spectrum synced to playback — or, when the current song has a downloaded Cinema video, the video itself.

- **Space** — play/pause
- **F11 / Alt+Enter** — toggle fullscreen (video or spectrum fills the screen edge to edge)
- **Escape** — exit fullscreen

### Cinema Video Support

Many maps ship a `cinema-video.json` for the [Cinema mod](https://github.com/Kevga/BeatSaberCinema), which plays a YouTube video behind the map in-game. The app supports these videos outside the game:

**Playback** — if the referenced video file is present in the song folder, the Visualizer plays it instead of the spectrum, seeked to stay in sync with the song's audio and honoring Cinema's configured offset and duration. Outside the video's window (before the offset, or after it ends), the spectrum shows instead. Playback uses libmpv embedded directly into the Visualizer window — hardware-accelerated, with pause/resume tracked frame-accurately against the audio — falling back to the spectrum if libmpv or the video is unavailable.

**Download** — the manifest often references a video you haven't downloaded in-game yet. Right-click the song → **Download Video** fetches it with yt-dlp using the same format and filename Cinema would (720p MP4, saved into the song folder), with download progress in the status bar. Failed downloads retry once automatically. Once finished, the video is immediately available in-game and in the Visualizer.

yt-dlp is looked for in Beat Saber's `Libs` folder (where Cinema keeps it), then next to the application — the same place as ffmpeg. If it isn't found, the app offers to download it for you.

The `{cinema}:y` search tag finds all songs with Cinema support — see [Search Tags](#search-tags).

---

## Media Keys

The player responds to system media keys while the app is running, regardless of which window has focus:

| Key | Action |
|---|---|
| Play/Pause | Toggle playback |
| Stop | Stop playback and clear queue |
| Next Track | Skip to next in queue |
| Previous Track | Go back in queue |

---

## Command Line

`Browser.py` accepts optional arguments for headless playlist operations and startup behavior.

```
python Browser.py [playlist] [--install] [--shuffle] [--randomAdd N [filter...]] ...
```

| Argument | Description |
|---|---|
| `playlist` | Path to a `.bplist` or `.json` playlist file. May not exist yet when combined with `--randomAdd` (the file is created). |
| `--install` | **Headless.** Download every missing song in the playlist directly from BeatSaver, then exit. Requires an existing `playlist` file and a resolvable `CustomLevels` folder. Takes precedence over `--shuffle` and `--randomAdd`, both of which are ignored. Exit code 0 on success, 1 on failure. |
| `--shuffle` | Shuffle song order. **Headless** when combined with a `playlist` arg: shuffles the playlist's songs (after any `--randomAdd` picks are appended) and writes the playlist back to disk. **GUI** when used with `--randomAdd` alone (no `playlist` arg): shuffles the startup queue. Requires either a `playlist` file or `--randomAdd`. |
| `--randomAdd N [filter...]` | Add N random songs from your library, optionally narrowed by search tags. **Headless** when combined with a `playlist` arg: appends picks to an existing playlist or writes a new playlist, then exits. **GUI** without a `playlist` arg: the picks become the startup queue (nothing is written to disk). Can be used multiple times to build composite picks. |

`--randomAdd` avoids duplicates when adding to an existing playlist (matched by song hash). When multiple `--randomAdd` groups are used, each group's picks are excluded from subsequent groups so there is no overlap.

### Headless vs. GUI

Every command is one of two modes, decided up front:

- **Headless** — runs to completion and exits. Use for scripted playlist edits. Triggers: `--install`, or any `playlist` arg combined with `--shuffle` and/or `--randomAdd`.
- **GUI** — launches the browser window. Triggers: a `playlist` arg by itself (loads it into the queue), `--randomAdd` without a `playlist` arg (picks become the queue), or no arguments at all.

### Pick Priority

Each `--randomAdd` group fills its slots in order — no repeats until a pool is exhausted:

1. **Filtered songs first** — random picks from songs matching the inline filters
2. **Unfiltered supplement** — if filtered results are fewer than N, remaining slots are filled from the rest of the library
3. **Repeats as last resort** — only if even the full library can't fill N slots

### Examples

```
python Browser.py playlist.bplist --install
```
Download every missing song in `playlist.bplist` directly from BeatSaver and exit.

```
python Browser.py --randomAdd 10 "{mapper}:Fefy"
```
Add 10 maps by Fefy to a queue (supplements from the full library if fewer than 10 exist).

```
python Browser.py --randomAdd 20 "{favorite}:y"
```
Create a queue of 20 favorite songs

```
python Browser.py --shuffle --randomAdd 5 "{favorite}:y" "{unplayed}:y" existing.bplist
```
Append 5 unplayed favorites to `existing.bplist`, shuffle it, save, and exit. If `existing.bplist` does not exist, it is created with the picks (still shuffled before saving).

```
python Browser.py --randomAdd 5 "{artist}:Miku" --randomAdd 5 "{artist}:Teto" --randomAdd 10 "{favorite}:y" --shuffle
```
Creates _objectively_ the best playlist: a queue with 5 Miku songs, 5 Teto songs, 10 user favorites, and finally shuffles before opening the UI and playing

```
python Browser.py --randomAdd 10 "{unplayed}:n" "{fc}:n" practice.bplist
```
Create (or append to) `practice.bplist` with 10 songs you've played at least once but haven't full combo'd yet, and exit.

## Licensing

The optional ffmpeg auto-download pulls a **GPL** static build from [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds). ffmpeg itself is downloaded and used as a standalone tool, not linked into this app, but if you redistribute a bundle that includes those binaries, the ffmpeg GPL terms apply to them. See [ffmpeg's license page](https://ffmpeg.org/legal.html) for details.
