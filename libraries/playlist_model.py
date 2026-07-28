"""Helpers for reading Beat Saber playlists (.bplist/.json) and matching their
entries against the installed song library.

Kept UI-free so both the browser window and the headless CLI can share them.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from libraries.song_data import SongInfo


def read_playlist(path) -> dict:
    """Load a playlist file. Raises OSError / JSONDecodeError on failure."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def entry_key(entry: dict) -> str:
    """The BeatSaver key/id for a playlist entry, or '' if it has neither."""
    return entry.get("key") or entry.get("id") or ""


def installable_entries(entries: list[dict]) -> list[dict]:
    """Entries that carry a BeatSaver key/id.

    This is the narrower of the two filters: it's what the per-song
    ``InstallManager`` fallback needs, since that path can only trigger an
    install by key. For the batch playlist installer use
    ``fetchable_entries``, which also accepts hash-only entries.
    """
    return [e for e in entries if entry_key(e)]


def fetchable_entries(entries: list[dict]) -> list[dict]:
    """Entries the playlist installer can download — a hash or a key is enough.

    Playlists exported by some tools carry only hashes; those are still
    fetchable via BeatSaver's ``/maps/hash`` endpoint, so they must not be
    filtered out the way ``installable_entries`` does.
    """
    return [e for e in entries if e.get("hash") or entry_key(e)]


def match_library(entries, songs) -> tuple[list["SongInfo"], list[dict]]:
    """Split playlist entries against the installed library by song hash.

    Returns ``(found, missing)``: ``found`` is the matched SongInfo objects (in
    playlist order); ``missing`` is the entry dicts with no installed match.
    """
    hash_to_song = {s.song_hash.upper(): s for s in songs if s.song_hash}
    found: list["SongInfo"] = []
    missing: list[dict] = []
    for entry in entries:
        song = hash_to_song.get((entry.get("hash") or "").upper())
        if song is not None:
            found.append(song)
        else:
            missing.append(entry)
    return found, missing
