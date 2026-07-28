"""playlist_model — .bplist reading and hash-matching against the library."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from libraries.playlist_model import (
    entry_key,
    installable_entries,
    match_library,
    read_playlist,
)


class TestReadPlaylist:
    def test_reads_json(self, tmp_path):
        p = tmp_path / "list.bplist"
        p.write_text(json.dumps({"playlistTitle": "T", "songs": []}), encoding="utf-8")
        assert read_playlist(p)["playlistTitle"] == "T"

    def test_raises_on_bad_json(self, tmp_path):
        p = tmp_path / "bad.bplist"
        p.write_text("{nope", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            read_playlist(p)

    def test_raises_on_missing_file(self, tmp_path):
        with pytest.raises(OSError):
            read_playlist(tmp_path / "absent.bplist")


class TestEntryKey:
    def test_key_then_id_then_empty(self):
        assert entry_key({"key": "abc1"}) == "abc1"
        assert entry_key({"id": "def2"}) == "def2"
        assert entry_key({"key": "", "id": "def2"}) == "def2"  # falsy key falls through
        assert entry_key({"hash": "AAA"}) == ""

    def test_installable_entries(self):
        entries = [{"key": "a"}, {"hash": "X"}, {"id": "b"}]
        assert installable_entries(entries) == [{"key": "a"}, {"id": "b"}]


class TestMatchLibrary:
    def _lib(self):
        return [
            SimpleNamespace(song_hash="AAA111", name="A"),
            SimpleNamespace(song_hash="bbb222", name="B"),  # lowercase on disk
            SimpleNamespace(song_hash="", name="NoHash"),
        ]

    def test_match_is_case_insensitive_both_directions(self):
        found, missing = match_library(
            [{"hash": "aaa111"}, {"hash": "BBB222"}], self._lib()
        )
        assert [s.name for s in found] == ["A", "B"]
        assert missing == []

    def test_preserves_playlist_order(self):
        found, _ = match_library(
            [{"hash": "BBB222"}, {"hash": "AAA111"}], self._lib()
        )
        assert [s.name for s in found] == ["B", "A"]

    def test_unmatched_and_hashless_entries_go_to_missing(self):
        entries = [{"hash": "ZZZ999"}, {"key": "abc1"}, {}]
        found, missing = match_library(entries, self._lib())
        assert found == []
        assert missing == entries

    def test_duplicate_entries_match_repeatedly(self):
        found, _ = match_library(
            [{"hash": "AAA111"}, {"hash": "AAA111"}], self._lib()
        )
        assert [s.name for s in found] == ["A", "A"]
