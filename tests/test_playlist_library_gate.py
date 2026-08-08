"""Playlist loads must wait for the first library scan.

Every playlist path decides what to download by diffing the playlist against
``self.songs``. That list starts empty and is filled by a background thread, so
a playlist opened during startup used to diff against nothing — ``match_library``
reported every entry as missing and the whole playlist went to the installer,
re-downloading songs that were already on disk.

The fix is a one-way gate: user-initiated playlist opens park themselves until
``_on_loaded`` runs, then replay against the real library.

Three properties are worth pinning:

  * A drop *before* the scan lands installs nothing, and the same drop replayed
    after the scan finds the songs already installed.
  * The gate is *initial-scan only*. A refresh or post-install reload replaces a
    library that is already populated, so nothing should defer then — deferring
    on every reload would stall the post-install requeue path.
  * A scan that dies in the worker releases what it parked instead of holding it
    forever, since ``_on_loaded`` will never run to drain it.

``FakeBrowser`` inherits the real mixin so the production gate, drain, and
``_on_loaded`` bookkeeping run against each other; only the Tk surface and the
downstream install/queue calls are stubbed.
"""

from __future__ import annotations

import pytest

from libraries.browser_playlists import BrowserPlaylistsMixin


class _StatusBar:
    def __init__(self):
        self.text = ""

    def config(self, text=""):
        self.text = text


class _Song:
    """Minimal SongInfo stand-in — the matcher only reads ``song_hash``."""

    def __init__(self, song_hash: str):
        self.song_hash = song_hash
        self.audio_path = "song.egg"


class FakeBrowser(BrowserPlaylistsMixin):
    def __init__(self, entries):
        self._entries = entries
        self.songs: list = []
        self._queue: list = []
        self._library_loaded = False
        self._pending_library_actions: list = []
        self._load_gen = 0
        self.status_bar = _StatusBar()
        self.count_label = _StatusBar()
        self.installed: list[list[dict]] = []   # one entry per installer launch
        self.queued: list[list] = []            # one entry per _play_queue call
        self._pending_playlist_entries = None
        self._pending_playlist_queue: list = []
        self._playlist_art_b64 = None
        self._playlist_art_locked = False
        self._playlist_art_first_song_key = None
        self._startup_playlist = None
        self._startup_random_groups: list = []
        self._startup_shuffle = False

    # ── Stubbed collaborators ────────────────────────────────────────────────

    def _do_search(self):
        pass

    def _open_startup_windows(self, _attempt: int = 0):
        pass

    def _notify_playlist_art_window(self):
        pass

    def _play_queue(self, songs):
        self.queued.append(list(songs))

    def _selected_folders_clear(self):
        pass

    def _play_audio(self, song):
        pass

    def _notify_queue_window(self):
        pass

    def _shuffle_queue_inplace(self):
        pass

    # _on_loaded touches these directly
    @property
    def _selected_folders(self):
        return set()

    @property
    def selected_indices(self):
        return set()


@pytest.fixture()
def playlist(tmp_path):
    """A 2-song .bplist on disk, plus the SongInfo list that satisfies it."""
    import json

    entries = [
        {"hash": "AAAA1111", "key": "1a", "songName": "One"},
        {"hash": "BBBB2222", "key": "2b", "songName": "Two"},
    ]
    path = tmp_path / "set.bplist"
    path.write_text(json.dumps({"playlistTitle": "Set", "songs": entries}))
    library = [_Song("aaaa1111"), _Song("BBBB2222")]
    return path, entries, library


def _browser(entries, monkeypatch):
    """FakeBrowser with the installer and dialogs stubbed to 'yes, install'."""
    fake = FakeBrowser(entries)

    class _Installer:
        def __init__(self, owner):
            self._owner = owner

        def install(self, _path, entries=None, installed_hashes=None):
            self._owner.installed.append(list(entries or []))
            return True

    fake._playlist_installer = _Installer(fake)
    monkeypatch.setattr(
        "libraries.browser_playlists.dialogs.ask_yes_no",
        lambda *a, **k: True,
    )
    return fake


def test_drop_during_scan_installs_nothing(playlist, monkeypatch):
    """The whole point: a playlist opened mid-scan must not hit the installer."""
    path, entries, library = playlist
    fake = _browser(entries, monkeypatch)

    fake._load_playlist_to_queue(str(path))

    assert fake.installed == [], "diffed against an empty library and re-installed"
    assert fake.queued == []
    assert len(fake._pending_library_actions) == 1
    assert "scanning" in fake.status_bar.text.lower()


def test_deferred_drop_replays_against_the_real_library(playlist, monkeypatch):
    """Once the scan lands, the parked open runs and finds everything present."""
    path, entries, library = playlist
    fake = _browser(entries, monkeypatch)

    fake._load_playlist_to_queue(str(path))
    fake._on_loaded(library)

    assert fake.installed == [], "already-installed songs were sent to BeatSaver"
    assert len(fake.queued) == 1 and len(fake.queued[0]) == 2
    assert fake._pending_library_actions == []


def test_missing_songs_still_install_after_the_scan(playlist, monkeypatch):
    """The gate delays the diff; it does not suppress a genuine install."""
    path, entries, _ = playlist
    fake = _browser(entries, monkeypatch)

    fake._load_playlist_to_queue(str(path))
    fake._on_loaded([_Song("AAAA1111")])   # second song genuinely absent

    assert len(fake.installed) == 1
    assert [e["hash"] for e in fake.installed[0]] == ["BBBB2222"]


def test_gate_is_initial_scan_only(playlist, monkeypatch):
    """A refresh/post-install reload must not re-arm the gate.

    ``self.songs`` is already populated across those reloads, so deferring then
    would stall the post-install requeue behind a scan that has nothing new to
    tell us.
    """
    path, entries, library = playlist
    fake = _browser(entries, monkeypatch)

    fake._on_loaded(library)          # initial scan
    fake._on_loaded(library)          # F5 / post-install reload
    fake._load_playlist_to_queue(str(path))

    assert fake._pending_library_actions == []
    assert len(fake.queued) == 1, "second open deferred instead of running"


def test_failed_scan_releases_parked_work(playlist, monkeypatch):
    """A worker that dies never reaches _on_loaded, so nothing would drain."""
    path, entries, _ = playlist
    fake = _browser(entries, monkeypatch)

    fake._load_playlist_to_queue(str(path))
    fake._on_load_failed(fake._load_gen, OSError("CustomLevels is gone"))

    assert fake._pending_library_actions == []
    assert "cancelled" in fake.status_bar.text
    assert fake.installed == []
    # Still ungated: the library remains unknown, so the next open re-defers
    # rather than diffing against a list that was never filled.
    assert fake._library_loaded is False


def test_stale_failure_is_ignored(playlist, monkeypatch):
    """A failure from a superseded scan must not cancel the current one's work."""
    path, entries, _ = playlist
    fake = _browser(entries, monkeypatch)

    fake._load_playlist_to_queue(str(path))
    fake._load_gen += 1
    fake._on_load_failed(0, OSError("old scan"))

    assert len(fake._pending_library_actions) == 1
