# Beat Saber Song Manager

A music browser, media player, playlist builder, and asset editor for Beat Saber custom maps. Browse your entire CustomLevels library, preview songs, manage favorites, shape playlists, and edit song files — all from one place.

---

## Setup

### Requirements

- **ffmpeg** — place `ffmpeg`/`ffmpeg.exe` and `ffprobe`/`ffprobe.exe` next to the application, or add them to your system PATH. [Download ffmpeg](https://ffmpeg.org/download.html) If it's missing when you convert audio, the app offers to download a prebuilt static build for you (from [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds), matching your platform — Windows or Linux), drops the binaries next to the app, and retries the conversion automatically — no manual download or PATH edit needed.
- **libmpv** — the preferred engine for audio and Cinema video playback. On **Windows**, place `libmpv-2.dll` next to the application or add it to PATH ([download](https://mpv.io/installation/), "libmpv" dev builds); if missing, the app offers to fetch it. On **Linux**, install it from your package manager: `sudo apt install libmpv2` (Debian/Ubuntu), `sudo dnf install mpv-libs` (Fedora), or `sudo pacman -S mpv` (Arch). If libmpv isn't present, the app falls back to ffmpeg for audio playback — see [Playback Engines](#playback-engines-mpv-and-ffmpeg) below.
- Beat Saber installed via Steam (recommended, but not required — see below). On Linux, Beat Saber runs through Steam Play/Proton; the app locates your library and reads scores/favorites from the game's Proton prefix automatically.

### Playback Engines: mpv and ffmpeg

The app leans on two media libraries, each for what it's best at, and neither is strictly mandatory — it degrades cleanly when one is absent.

**libmpv is the preferred engine.** It plays the song audio in-process, with live volume, pause, and seek that apply instantly without relaunching anything. More importantly, it's what makes Cinema video support tractable: playing a map's video behind its audio means running two media streams that stay frame-accurately in sync through pauses, seeks, and offsets. libmpv handles that overlay natively — its pause property freezes the video clock in lockstep with the audio, so there's no manual reseek dance. Doing the same by hand over raw ffmpeg processes would mean orchestrating multiple subprocesses and threads and constantly correcting drift; libmpv collapses all of that into one embedded player. That's why it's the default for both audio and video.

**ffmpeg covers conversion and visualizer flair, and serves as the playback fallback.** ffmpeg is already useful elsewhere in the app — it converts replacement audio to Beat Saber's native `.ogg`/`.egg` on import and renders the real-time frequency-bar spectrum in the Visualizer. Because it's a common tool that many users already have installed or on their PATH for other reasons, it's a reasonable thing to assume might already be present. That makes it a natural fallback: when libmpv is missing but ffmpeg is, audio plays through ffmpeg's bundled `ffplay` instead, and the Visualizer shows its spectrum bars. The trade-off in this mode is that Cinema video is unavailable (that overlay genuinely needs libmpv) and volume changes relaunch playback at the current position rather than applying live. No prompt interrupts you — the app just uses what's there.

**Track durations** (for the progress bar and queue) are read by a built-in metadata reader that ships with the app — no external binary required — so they work whichever engine is present, or even with neither installed. `ffprobe` and libmpv act only as fallbacks for the rare file the built-in reader can't parse; if both of those are missing when one is actually needed, the app offers to install or repair ffmpeg.

**When both are missing**, the app offers to download libmpv (once per run). If you decline, there's no engine available to play the audio, so the song is skipped with a message explaining what's missing. (Beat Saber audio is `.egg`/`.ogg` — formats most systems don't associate with any player — so handing the file to your OS isn't a dependable fallback, and the app doesn't try.)

See [Design Notes → Dependency Degradation](UX.md#dependency-degradation) for the full reasoning behind the fallback ladder.

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
- **Add Cinema Video…** — appears when a song has no `cinema-video.json` at all. Paste a YouTube link and the app downloads the video and writes the config for you.
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
- **Replace Audio** — file picker for common audio formats including Beat Saber's native `.ogg`/`.egg`, then an alignment editor. The replacement's waveform is drawn under the current audio's on a shared timeline; drag it, nudge it with ← / → (Shift ×100 ms, Ctrl ×1000 ms), or hit **Detect Silence** to line up where the music starts in each. Scroll the wheel over either waveform to move the playhead, or right-click to drop it where you point; clicking the overview strip up top jumps the view and takes the playhead to the start of it, the view pans to follow the playhead, and it stops at the start of the song and at the end of whatever the write will produce. **Preview** plays them split across your ears — current audio left, replacement right — so a misalignment is audible as a flam; the dropdown next to it solos either one for comparing takes rather than timing. Gaps are filled with silence, so a replacement that starts later or runs short never leaves the chart with notes past the end of the audio. **Match the original length** (on by default) also trims a longer replacement back; switch it off to keep an extended mix whole. Everything is converted to OGG on write, and the original is backed up as before.
- **Edit Info** — shown in red with a warning. Editing metadata changes the song's SHA1 hash, which breaks its identity on BeatSaver (install links, playlist matching). Fine for personal use; avoid if you plan to share the map.
- **Custom Tags…** — add or remove personal tags on a song (or a multi-selection). Tags are searchable via `{custom}:tagname`.
- **Clear Score** — removes score data for this song only. All other high scores are preserved.
- **Restore from Backup** — reverts to the backup created at first edit.

### Search Tags

All tags use `{tag}:value` syntax and are case-insensitive. Multiple tags can be combined in one query (space-separated). Plain text without a tag searches title, artist, mapper, and BeatSaver ID simultaneously.

**Right-click the search bar** for **Cut**, **Copy** (both greyed out unless text is selected), **Paste**, and **Add tag…** — a list of every tag below. Picking a tag appends it to the query and puts the cursor after the colon, ready for the value. Yes/no tags arrive prefilled with `y`.

Ctrl+X / Ctrl+C / Ctrl+V and Ctrl+A (select all) work in the search bar and in the Add Random filter box.

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
- **Right-click** — View Queue, View Song, Save Image…, plus the Cinema action that fits the current song: **Add Cinema Video…** when it has no config, **Download Video** when it has one whose video isn't downloaded, or **Cinema Offset…** when the video is there (Shift+right-click adds **Replace Cinema Video…**, as in the song list). Picking any of them drops out of fullscreen, so the dialog it opens isn't stranded behind the window.

### Cinema Video Support

Many maps ship a `cinema-video.json` for the [Cinema mod](https://github.com/Kevga/BeatSaberCinema), which plays a YouTube video behind the map in-game. The app supports these videos outside the game:

**Playback** — if the referenced video file is present in the song folder, the Visualizer plays it instead of the spectrum, seeked to stay in sync with the song's audio and honoring Cinema's configured offset and duration. Outside the video's window (before the offset, or after it ends), the spectrum shows instead. Playback uses libmpv embedded directly into the Visualizer window — hardware-accelerated, with pause/resume tracked frame-accurately against the audio — falling back to the spectrum if libmpv or the video is unavailable.

**Download** — the manifest often references a video you haven't downloaded in-game yet. Right-click the song (in the list, or in the Visualizer) → **Download Video** fetches it with yt-dlp using the same format and filename Cinema would (720p MP4 by default, saved into the song folder — see **Quality** below), with download progress in the status bar. Failed downloads retry once automatically. Once finished, the video is immediately available in-game and in the Visualizer.

**Add your own** — for a song with no `cinema-video.json`, right-click → **Add Cinema Video…** and paste a YouTube link. Normally this means launching the game and searching from Cinema's in-game menu; here it's one paste. The dialog prefills from your clipboard if you've already copied a link, and accepts every form YouTube hands out — `watch?v=`, `youtu.be/`, Shorts, embeds, or a bare video ID.

The app fetches the video's title, channel and duration with yt-dlp, downloads the video, and only then writes a `cinema-video.json` — so a failed download leaves nothing behind. The config records exactly the fields Cinema itself writes for a video you pick in-game, using the same filename the mod would derive, so the video is immediately playable in Beat Saber without re-downloading. A link with a timestamp (`&t=1m30s`) seeds the offset, so the video starts where you pointed at it.

The offset editor (Shift+right-click → **Cinema Offset…**) opens automatically afterwards — a config created from scratch starts at offset 0 and is essentially never in sync. It draws the song's waveform above the video's on a shared timeline: drag the video strip or nudge it with ← / → (Shift ×100 ms, Ctrl ×1000 ms), scroll the wheel over either waveform to move the playhead — right-click drops it where you point, clicking the overview strip up top jumps the view and takes the playhead to the start of it, the view pans to follow, and it stops at the start and end of the song — and **Preview** plays from the playhead with the offset applied live. Syncing a config you just created doesn't leave a `.bak`: there's no earlier version to restore to, and the file has never been anything but yours.

**Replace** — hold Shift and right-click a song that already has a video for **Replace Cinema Video…** — in the song list or in the Visualizer. This confirms first: a mapper's config carries screen placement, colour correction and environment changes that can't be reconstructed from a YouTube link. The original is backed up, so **Restore Files** undoes it.

**Quality** — downloads default to the 720p H.264 stream Cinema itself fetches, so the file is what the mod would have produced. To go higher, set `cinema_video_quality` in `config.json` (in `%APPDATA%\BeatSaberSongManager` on Windows) to `"1080"` or `"max"`; the key is written into the file automatically, so it's there to edit. Both raised settings take the best resolution a video actually publishes rather than failing when it doesn't go that high.

`"1080"` is the highest setting that never re-encodes. YouTube publishes H.264 up to 1080p and nothing above it, so anything higher has to be converted on the way in — which is what `"max"` will do, but only when it buys something:

| The video's best stream | `"max"` downloads | Re-encodes? |
| --- | --- | --- |
| 1080p (the common case) | 1080p H.264 | No — identical to `"1080"` |
| 1440p, or 4K at 30fps | that resolution, in VP9 | Yes, to H.264 |
| 4K at 60fps | 1440p60 | Yes, to H.264 |

4K60 is capped on purpose. H.264's level is set by macroblocks *per second*, so 4K at 30fps lands in level 5.1 while 4K at 60fps needs 5.2 — and the Media Foundation decoder Cinema ends up on [documents support only up to 5.1](https://learn.microsoft.com/en-us/windows/win32/medfound/h-264-video-decoder). 1440p60 is the most resolution that still fits. (4K30 fits by 1.2%.)


The conversion is needed because Cinema plays video through Unity's `VideoPlayer`, which decodes H.264, H.265 and VP8 — [not VP9 or AV1](https://docs.unity3d.com/6000.3/Documentation/Manual/video-encoding-compatibility.html). That's why the mod's own downloader hardcodes `vcodec*=avc1`, and why its in-game quality setting stops at 1080p with 1440p and 2160p commented out of the source. Left as VP9 or AV1, a 4K download would play fine in the Visualizer and show a black screen behind the map.

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
python Browser.py [playlist] [--playlist PATH] [--install] [--shuffle] [--randomAdd N [filter...]] ...
```

| Argument | Description |
|---|---|
| `playlist` | Path to a `.bplist` or `.json` playlist file. May not exist yet when combined with `--randomAdd` (the file is created). |
| `--playlist PATH` | The same playlist file, named explicitly. Identical in every other way — see [Naming the playlist](#naming-the-playlist) for when you need it. Wins over the positional form if both are given. |
| `--install` | **Headless.** Download every missing song in the playlist directly from BeatSaver, then exit. Requires an existing playlist file and a resolvable `CustomLevels` folder. Takes precedence over `--shuffle` and `--randomAdd`, both of which are ignored. Exit code 0 on success, 1 on failure. |
| `--shuffle` | Shuffle song order. **Headless** when combined with a playlist: shuffles the playlist's songs (after any `--randomAdd` picks are appended) and writes the playlist back to disk. **GUI** when used with `--randomAdd` alone (no playlist): shuffles the startup queue. Requires either a playlist file or `--randomAdd`. |
| `--randomAdd N [filter...]` | Add N random songs from your library, optionally narrowed by search tags. **Headless** when combined with a playlist: appends picks to an existing playlist or writes a new playlist, then exits. **GUI** without a playlist: the picks become the startup queue (nothing is written to disk). Can be used multiple times to build composite picks. |

`--randomAdd` avoids duplicates when adding to an existing playlist (matched by song hash). When multiple `--randomAdd` groups are used, each group's picks are excluded from subsequent groups so there is no overlap.

### Naming the playlist

`--randomAdd` takes an open-ended list of filters, so when a bare playlist path follows it the two are hard to tell apart. A path is identified by its `.bplist`/`.json` extension and pulled out of the filter list, which is what makes this work:

```
python Browser.py --randomAdd 3 out.bplist
```

The catch is that filters can be plain text, and plain text ending in `.json` looks exactly like a path. `--randomAdd 5 foo.json` is read as "write a playlist named `foo.json` with 5 unfiltered picks" rather than "5 songs matching the text `foo.json`" — a silent switch into headless mode. `--playlist` removes the guesswork:

```
python Browser.py --playlist out.bplist --randomAdd 5 foo.json
```

Tagged filters like `"{title}:foo.json"` are always recognised as filters, so this only affects untagged plain-text searches. Use `--playlist` in scripts regardless — it says what it means.

### Headless vs. GUI

Every command is one of two modes, decided up front:

- **Headless** — runs to completion and exits. Use for scripted playlist edits. Triggers: `--install`, or any playlist argument combined with `--shuffle` and/or `--randomAdd`.
- **GUI** — launches the browser window. Triggers: a playlist argument by itself (loads it into the queue), `--randomAdd` without a playlist (picks become the queue), or no arguments at all.

Either spelling of the playlist — positional or `--playlist` — decides the mode identically.

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

```
python Browser.py --playlist practice.bplist --randomAdd 10 "{unplayed}:n" "{fc}:n"
```
The same command with the playlist named explicitly — the form to prefer in scripts.

## Licensing

The optional ffmpeg auto-download pulls a **GPL** static build from [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds). ffmpeg itself is downloaded and used as a standalone tool, not linked into this app, but if you redistribute a bundle that includes those binaries, the ffmpeg GPL terms apply to them. See [ffmpeg's license page](https://ffmpeg.org/legal.html) for details.
