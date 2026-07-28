# Design Notes — Beat Saber Song Manager

Intended audience: contributors, anyone extending the app, or anyone who wants to understand the *why* behind the interaction model rather than just the *what*.

---

## Visual Grammar

Three states, applied consistently across every window and every control:

- **White** — interactive. If it's white, you can click it.
- **Grey** — inactive. Not available in the current state, not a permanent restriction.
- **Highlighted** — an indicator. Something worth looking at.

This gives users a reliable contract: color encodes state, not decoration. A user who learns this in the main window can apply it immediately in the Queue window or anywhere else. It also makes it easy to see at a glance what's actionable and what isn't, without reading labels.

---

## The Shift Modifier

Shift is the consistent escalation key throughout the app. It means "I mean it" — unlocking elevated or otherwise gated actions that would carry too much risk on a plain click.

- **Shift+right-click** — opens the edit menu (asset editing, score clearing, deletes on protected songs)
- **Shift+right-click → Play Now** — jumps the queue instead of queuing at the end
- Favorited songs are protected from deletion by default; Shift removes that protection

The goal is that users who discover the Shift pattern in one context can generalize it to others. It's a consistent modifier, not a one-off workaround.

---

## Friction Calibration

The app calibrates confirmation prompts to two factors: **consequence** and **intentionality**. The principle is that experienced users who know what they're doing should rarely be interrupted, while newer users should be protected from expensive mistakes.

### Intentionality signals

Multi-step keyboard sequences imply deliberateness and get no confirmation:
- Ctrl+A then Delete in the Queue window clears the queue immediately — you had to select everything first, so it was on purpose.

Single button clicks on destructive actions get a confirmation guard:
- The Queue button opens a "clear queue?" dialog — one mis-click shouldn't wipe your playlist.

The Shift modifier explicitly signals elevated intent, which is why the edit menu sits behind it.

### Consequence levels

| Level | Treatment |
|---|---|
| High consequence, no recovery | Red UI + explanatory warning (e.g., song info editing changes SHA1 hash) |
| High consequence, recoverable | Backup created automatically; no friction added to the action itself |
| Lower consequence | No friction |

The SHA1 warning on song info editing is the one case where the app actively tells you *why* something is risky, not just that it is. The mechanism matters: editing breaks BeatSaver lookup and playlist hash matching. Personal edits are fine; distribution breaks.

---

## Backup Philosophy

Backups are created **on first edit only**. This ensures the backup is always a clean copy of the original — not a snapshot of something already modified by a previous edit. Every shift+right-click operation that touches a file follows this rule.

Restore is available from the same menu where the edit was made. The place you break something is the place you fix it.

---

## Multiple Paths

Where possible, the same action is reachable through multiple interaction styles, because different users work differently — and the same user works differently at different times.

| Style | User |
|---|---|
| Point-and-click menus and buttons | Mouse-first users, new users |
| Keyboard shortcuts | Power users, people who want to stay on the keyboard |
| Hardware media keys | Users not looking at the window (across the room, other monitor) |
| Drag and drop | Tactile, visual interactions for file operations |

The goal is not to build three separate UIs but to make sure the common paths in each style are complete. A user controlling playback from a couch with media keys shouldn't need to touch the mouse. A user building playlists through search shouldn't need to open a menu.

---

## Window Design Philosophy

**Main window** is the library. Full metadata, scores, mapper info, search, cover art. The place you discover, manage, and curate songs.

**Queue window** strips non-playback information — mapper names, score data, difficulty tags — and focuses on playlist shape and playback order. It's a media player view of the same data.

These serve different mental modes. Curation happens in the main window. Listening happens in the queue window. The main window can be minimized entirely if the user just wants to play music — the queue window and hardware media keys are sufficient for that workflow.

**Information density per context:** show what's relevant to the task at hand.

---

## The Internal Clipboard

Cut/copy/paste in the Queue window uses an internal clipboard independent of the system clipboard. This avoids clobbering whatever the user has copied in another app. The trade-off is that songs can't be pasted outside the app — but queue items aren't meaningful outside the app anyway.

Cut state is communicated with a dark-red tint on affected rows, consistent with the visual grammar (highlighted = something to look at). Closing the Queue window clears the tint but preserves the clipboard so paste still works on reopen.

---

## Progressive Enhancement

The app works without Beat Saber installed at all. If the Steam path isn't found and there's no score file, it will ask for a CustomLevels folder path directly. Even a folder of Beat Saber maps with no game present is enough to use it as a music player, playlist builder, and to install songs — maps download straight from BeatSaver into whatever CustomLevels folder the app is pointed at.

---

## Dependency Degradation

The playback stack depends on external media libraries — libmpv and ffmpeg — that may or may not be present on a given machine. The governing principle is that a missing optional dependency should **degrade a feature, never brick the app**. A user who launches without libmpv should not meet a crash, a blank Visualizer, or a frozen progress bar; they should get a slightly lesser version of the same experience, ideally without even being interrupted to fix it.

This is deliberately the opposite of a catastrophic-failure model, where one absent library takes the whole feature — or the whole app — down with it. Each capability is designed to fall to the next-best implementation on its own, and the fallbacks are ordered so the drop in quality at each step is as small as possible.

### The playback ladder

| Available | Audio | Cinema video | Visualizer | Progress bar | Prompt? |
|---|---|---|---|---|---|
| libmpv (+ ffmpeg) | libmpv, live controls | Yes, synced overlay | Spectrum (ffmpeg) | built-in reader (live mpv when playing) | — |
| ffmpeg only | ffplay subprocess | — (needs libmpv) | Spectrum (ffmpeg) | built-in reader | No |
| neither | none — skip with message | — | — (nothing plays) | — (nothing plays) | Offer libmpv download |

The key calibration is the middle row: **falling back should be silent when the result is still good.** ffmpeg is already a hard requirement elsewhere in the app and is a tool many users happen to have on their PATH anyway, so the ffplay fallback is a likely-available, genuinely usable path — not a broken one. Interrupting the user with a "libmpv missing" dialog when their music is about to play fine through ffmpeg would be friction without payoff. The prompt is reserved for the bottom row, where there's no in-app playback engine at all and the user actually needs to act.

The bottom row deliberately stops at a clear message rather than reaching for a system-level fallback. Handing the audio file to the OS default player isn't dependable — Beat Saber audio is `.egg`/`.ogg`, formats most systems don't associate with any player, so it would mostly produce an error or silence, which is worse than an honest "install libmpv to play audio." A degradation ladder is only worth having if every rung actually works. Because nothing plays in this state, the Visualizer and progress bar don't degrade to a lesser view — they simply never initialize, since there's no playback for them to track; the em dashes in that row mean "does not appear," not "shows a reduced version."

Track durations sit slightly apart from this ladder. They're read by a built-in, pure-Python metadata reader (mutagen) that parses the file header directly, so the progress bar and queue get a length in every row where something plays — no external binary needed. `ffprobe` and libmpv are consulted only as ordered fallbacks for the occasional file the reader can't parse; when the progress bar shows during libmpv playback it prefers the live player's own duration. Only if a file needs a fallback *and* both ffprobe and libmpv are absent does the app surface a one-per-run prompt — repairing an incomplete ffmpeg if it's present, or offering a fresh ffmpeg install if it isn't.

One case is deliberately kept separate from those two: if the built-in reader itself can't be loaded, no external tool will help, because every duration read in the app fails before it ever reaches a fallback. That's a broken install rather than a misconfigured one, so it gets its own informational prompt naming mutagen and how to restore it — never an ffmpeg prompt, which would send the user round in circles reinstalling something that was never the problem. The reader is asked for only the four formats the app documents (Ogg Vorbis, MP3, WAV, M4A) for the same reason: mutagen's format sniffing imports two dozen format modules when left unscoped, and in a packaged build one missing module would otherwise take every duration read down with it.

The same logic runs *within* the Visualizer: a Cinema video plays only while its own window (offset to offset-plus-duration) is active and libmpv is available; outside that window, or without libmpv, it drops to the spectrum; without ffmpeg, it drops again to a static cover-art background rather than going blank. Every rung is a smaller, self-contained step down, so the failure of any one piece is contained to the smallest possible loss of function.

This mirrors the [Progressive Enhancement](#progressive-enhancement) stance on Beat Saber itself: the richest experience when everything is present, a coherent and useful experience when it isn't, and no hard dependency wall that turns a missing extra into a dead end.

---

## Escape Hatches

The app consistently guides users toward external tools rather than trying to replace them:

- "Open in file explorer" shortcuts for direct file system access
- ctrl+click a song title or art to view the song on BeatSaver
- Command line features expose the playlist manipulation logic for users who want scripted or automated workflows

This keeps scope tight and lets the app focus on what it does well — browsing, playback, and playlist management — while making it easy to reach the right tool for everything else.
