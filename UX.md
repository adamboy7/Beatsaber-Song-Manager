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

## Escape Hatches

The app consistently guides users toward external tools rather than trying to replace them:

- "Open in file explorer" shortcuts for direct file system access
- ctrl+click a song title or art to view the song on BeatSaver
- Command line features expose the playlist manipulation logic for users who want scripted or automated workflows

This keeps scope tight and lets the app focus on what it does well — browsing, playback, and playlist management — while making it easy to reach the right tool for everything else.
