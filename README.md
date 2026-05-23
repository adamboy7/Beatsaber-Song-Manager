# Beat Saber Song Browser

A custom song manager, media player, asset editor, and playlist creator for Beat Saber. Browse and search your entire custom song library, play previews, manage favorites, build playlists, and edit song assets — all from one window.

---

## Features

- **Song Browser** — Paginated list of your entire CustomLevels library with cover art, BPM, BeatSaver ID, and date added
- **Advanced Search** — Tag-based filtering by title, artist, mapper, play status, and favorites; combinable with plain-text search
- **Media Player** — Audio preview playback with queue, shuffle, loop, and media key support
- **Favorites** — Add/remove favorites synced directly with Beat Saber's PlayerData.dat
- **Score Display** — Per-difficulty high scores, ranks, play counts, and full combo status read from your save data
- **Playlist Management** — Import `.bplist` playlists from file or by drag-and-drop; export selections or queue as new playlists
- **Asset Editor** — Replace a song's cover art or audio file, with automatic backup and OGG conversion
- **Mod Assistant Integration** — One-click install of songs from BeatSaver URLs directly within the browser

---

## Requirements

- Beat Saber installed via Steam (the CustomLevels folder is auto-detected from the Steam registry)
- `ffplay.exe` / `ffmpeg.exe` / `ffprobe.exe` bundled alongside the app (required for audio playback and conversion)

Python dependencies: `Pillow`, `pynput`, `pydub`, `TkinterDnD2`

---

## Basic Usage

### Browsing Songs

Launch `Browser.py`. Your CustomLevels folder is detected automatically. Songs load in the background and appear in a paginated list (default 50 per page).

- **Right-click the page indicator** to change the number of results per page
- **Right-click Prev/Next** to jump directly to a page number
- **F5** refreshes the song library and scores

### Playing Songs

- **Left-click** a song to select it; **right-click** for the context menu
- Choose **Play** to start immediately, or **Add to Queue** to queue it
- The player bar appears at the bottom when **Options → Show Media Player** is enabled
- **Spacebar** toggles play/pause when the search bar is not focused

### Viewing Scores

Each song row shows per-difficulty scores, ranks, and play counts pulled live from your Beat Saber save data. A gold ★ indicates the song is favorited in-game, and colored text denotes a full combo.

### Favorites

Right-click a song and choose **Add to Favorites** or **Remove from Favorites**. Multi-select works too — bulk-add or bulk-remove across any number of songs at once.

> **Deletion protection:** Favorited songs cannot be deleted by default. To delete a favorited song, hold **Shift** while right-clicking (single or multi-select) to unlock the delete option.

### Importing Playlists

- **File → Open Playlist** to browse for a `.bplist` file
- **Drag and drop** a `.bplist` file onto the browser window
- If the queue already has songs, you will be prompted to **Overwrite** or **Append** — choose **Append** to combine two playlists into one queue
- Missing songs in the playlist can be installed automatically via Mod Assistant

### Exporting Playlists

Select multiple songs (Shift+click for range, Ctrl+A for all visible), then right-click and choose **Share Playlist** to save them as a new `.bplist` file. Alternatively in the Queue window, right-click and select **Save Queue**

### Editing Assets

**Shift+right-click** a song to access asset editing options:

- **Replace Cover Art** — Select a new image; it will be resized to match the original dimensions. The original is backed up as `.bak`.
- **Replace Audio** — Select an audio file (OGG, MP3, WAV, FLAC). Non-OGG files are converted automatically. The original is backed up as `.bak`.
- **Restore from Backup** — Reverts to the `.bak` file if one exists.
- **Clear Scores** — Removes all score data for this song from your save file (a backup of PlayerData.dat is created first).

### Installing Songs from BeatSaver

Paste a BeatSaver URL (`https://beatsaver.com/maps/ID`) or a `beatsaver://` link into the search bar. An install row appears — press **Enter** or click it to install via Mod Assistant. The browser reloads and re-applies your search automatically after installation. If you do not have mod assistant set up properly, the Song Browser can download the latest release and help you set up one click installs.

---

## Search Tags

All tags use the syntax `{tag}:value` and are **case-insensitive**. Multiple tags can be combined in a single query (space-separated). Plain text without a tag searches across title, artist, mapper, and BeatSaver ID simultaneously.

| Tag | Values | Description |
|-----|--------|-------------|
| `{title}:TEXT` | any text | Filter by song name (substring match) |
| `{artist}:TEXT` | any text | Filter by song artist (substring match) |
| `{mapper}:TEXT` | any text | Filter by mapper name (substring match) |
| `{unplayed}:y` | `y` | Show only songs with zero plays across all difficulties |
| `{unplayed}:n` | `n` | Show only songs that have been played at least once |
| `{favorite}:y` | `y` | Show only favorited songs |
| `{favorite}:n` | `n` | Show only non-favorited songs |

### Examples

```
{mapper}:psi {unplayed}:y
```
Unplayed songs mapped by Psi.

```
{artist}:camellia {favorite}:n
```
Non-favorited Camellia songs.

```
{title}:escape
```
All songs with "escape" in the title.

```
camellia
```
Plain-text search across title, artist, mapper, and song ID.

> **Note:** The **View** menu also has quick toggles for **Favorites Only** and **Hide Favorites** that work independently of the search bar.

---

## Queue & Playback

- **Add to Queue** (context menu or multi-select) adds songs to the playback queue
- **View → Queue** opens the Queue window, which shows thumbnails and allows drag-to-reorder
- In the Queue window, **Delete/Backspace** removes selected entries
- **Shuffle** randomizes the remaining queue; **Loop** repeats the queue after the last song

---

## Media Keys

The player responds to system media keys while the app is running:

| Key | Action |
|-----|--------|
| Play/Pause | Toggle playback |
| Stop | Stop playback (clears queue)|
| Next Track | Skip to next in queue |
| Previous Track | Go back in queue |

---

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Space` | Play / Pause (player must be visible and search bar unfocused) |
| `F5` | Refresh song library |
| `Escape` | Deselect all |
| `Ctrl+A` | Select all visible songs |
| `Ctrl+Click` | Open song's BeatSaver page in browser |
| `Shift+Click` | Range-select songs |
| `Delete` / `Backspace` | Remove selected |
| `Enter` | Confirm pending song install (when install row is shown) |

---

## Command Line Interface

`Browser.py` accepts optional arguments for headless playlist operations and startup behaviour.

```
python Browser.py [playlist] [--shuffle] [--randomAdd N [filter...]] ...
```

| Argument | Description |
|----------|-------------|
| `playlist` | Path to a `.bplist` or `.json` playlist file (optional) |
| `--shuffle` | **Headless.** Shuffle the songs in the given playlist file in place and exit. Requires `playlist`. |
| `--randomAdd N [filter...]` | Add `N` random songs from your library, optionally narrowed by one or more search tags (see [Search Tags](#search-tags)). Can be used multiple times to build composite picks. |

### `--randomAdd` behaviour

| Command | Effect |
|---------|--------|
| `python Browser.py --randomAdd 10` | Opens the GUI and pre-populates the queue with 10 random songs. |
| `python Browser.py --randomAdd 10 new.bplist` | **Headless.** Creates `new.bplist` with 10 random songs and exits (file must not already exist). |
| `python Browser.py --shuffle --randomAdd 5 existing.bplist` | **Headless.** Adds 5 random songs to `existing.bplist`, shuffles the full list, writes it back, and exits. |
| `python Browser.py --shuffle existing.bplist` | **Headless.** Shuffles `existing.bplist` in place and exits. |

`--randomAdd` avoids duplicates when adding to an existing playlist (matched by song hash). When multiple `--randomAdd` groups are used, each group's picks are excluded from subsequent groups so there is no overlap.

### Pick priority

Each `--randomAdd` group fills its N slots in priority order — no repeats until a pool is fully exhausted:

1. **Filtered songs first** — random picks from songs matching the inline filters (no repeats).
2. **Unfiltered supplement** — if filtered results < N, remaining slots are filled from the rest of the library (also no repeats).
3. **Repeats as last resort** — only if even the full unfiltered library cannot fill N slots.

If the filter matches zero songs a warning is printed and all N picks come from the full library.

### Examples

```
python Browser.py --randomAdd 10 "{mapper}:Fefy" playlist.bplist
```
Add 10 songs mapped by Fefy to a playlist (supplements with other songs if fewer than 10 exist).

```
python Browser.py --randomAdd 20 "{unplayed}:y" new.bplist
```
Create a new playlist of 20 unplayed songs.

```
python Browser.py --shuffle --randomAdd 5 "{favorite}:y" "{unplayed}:y" existing.bplist
```
Add 5 unplayed favorites to an existing playlist, then shuffle it.

```
python Browser.py --randomAdd 15 "{artist}:camellia"
```
Open the GUI and pre-load 15 Camellia songs into the queue.

```
python Browser.py --randomAdd 5 "{artist}:Miku" --randomAdd 5 "{artist}:Teto" new.bplist
```
Create a playlist with 5 Miku songs and 5 Teto songs, with no overlap between groups.

---