"""fs_utils, steam_paths, and the hash cache in load_song_hashes."""

from __future__ import annotations

import json

import pytest

from conftest import make_song_folder, v2_info

from libraries.fs_utils import atomic_write_text
from libraries.steam_paths import parse_vdf_library_paths


class TestAtomicWriteText:
    def test_writes_and_overwrites(self, tmp_path):
        target = tmp_path / "out.json"
        atomic_write_text(target, "one")
        atomic_write_text(target, "two")
        assert target.read_text(encoding="utf-8") == "two"
        # No stray temp files left behind.
        assert list(tmp_path.iterdir()) == [target]

    def test_failure_leaves_destination_untouched(self, tmp_path):
        target = tmp_path / "out.json"
        target.write_text("original", encoding="utf-8")
        with pytest.raises(UnicodeEncodeError):
            atomic_write_text(target, "bad \ud800 surrogate")
        assert target.read_text(encoding="utf-8") == "original"
        assert list(tmp_path.iterdir()) == [target]  # temp cleaned up


class TestParseVdf:
    def test_returns_only_existing_paths(self, tmp_path):
        lib_a = tmp_path / "SteamLibraryA"
        lib_a.mkdir()
        vdf = tmp_path / "libraryfolders.vdf"
        vdf.write_text(
            '"libraryfolders"\n{\n'
            f'  "0"\n  {{\n    "path"    "{lib_a}"\n  }}\n'
            f'  "1"\n  {{\n    "path"    "{tmp_path / "Missing"}"\n  }}\n'
            "}\n",
            encoding="utf-8",
        )
        assert parse_vdf_library_paths(vdf) == [lib_a]

    def test_missing_vdf_is_empty(self, tmp_path):
        assert parse_vdf_library_paths(tmp_path / "absent.vdf") == []


class TestHashCache:
    """load_song_hashes computes, caches by folder mtime, and invalidates."""

    @pytest.fixture()
    def custom_levels(self, tmp_path, monkeypatch):
        # Layout: <root>/Beat Saber_Data/CustomLevels so UserData resolves
        # under <root> (no SongCore file present → pure fallback path).
        root = tmp_path / "game"
        levels = root / "Beat Saber_Data" / "CustomLevels"
        levels.mkdir(parents=True)
        cache_file = tmp_path / "hash_cache.json"
        from libraries import app_config
        monkeypatch.setattr(app_config, "hash_cache_path", lambda: cache_file)
        return levels, cache_file

    def test_computes_caches_and_invalidates(self, custom_levels):
        import os
        from libraries.song_data import compute_song_hash, load_song_hashes

        levels, cache_file = custom_levels
        folder = make_song_folder(levels, "s1 (A - B)", v2_info(title="A"))

        result = load_song_hashes(levels)
        expected = compute_song_hash(folder)
        assert result == {"s1 (A - B)": expected}
        cached = json.loads(cache_file.read_text())
        assert cached["s1 (A - B)"]["hash"] == expected

        # Poison the cache to prove an unchanged folder is served from it.
        cached["s1 (A - B)"]["hash"] = "CACHED"
        cache_file.write_text(json.dumps(cached))
        assert load_song_hashes(levels)["s1 (A - B)"] == "CACHED"

        # Touch a difficulty file → mtime changes → recompute, not cache.
        diff = folder / "ExpertStandard.dat"
        os.utime(diff, (diff.stat().st_atime, diff.stat().st_mtime + 10))
        assert load_song_hashes(levels)["s1 (A - B)"] == expected

    def test_songcore_file_takes_priority(self, custom_levels):
        from libraries.song_data import load_song_hashes

        levels, _ = custom_levels
        make_song_folder(levels, "s1 (A - B)", v2_info(title="A"))
        songcore = levels.parent.parent / "UserData" / "SongCore"
        songcore.mkdir(parents=True)
        (songcore / "SongHashData.dat").write_text(json.dumps({
            "some\\path\\s1 (A - B)": {"songHash": "deadbeef"},
        }))
        # SongCore's (uppercased) hash wins; no fallback compute for it.
        assert load_song_hashes(levels)["s1 (A - B)"] == "DEADBEEF"
