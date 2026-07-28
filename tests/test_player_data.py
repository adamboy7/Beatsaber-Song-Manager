"""player_data — PlayerData.dat parsing, level-id derivation, stat merging."""

from __future__ import annotations

import json
from types import SimpleNamespace

from libraries.player_data import (
    DiffStat,
    get_song_stats,
    load_favorites,
    load_player_stats,
    song_level_ids,
)


def _player_dat(tmp_path, levels=None, favorites=None):
    p = tmp_path / "PlayerData.dat"
    p.write_text(json.dumps({
        "localPlayers": [{
            "favoritesLevelIds": favorites or [],
            "levelsStatsData": levels or [],
        }]
    }), encoding="utf-8")
    return p


class TestLoadFavorites:
    def test_reads_first_player(self, tmp_path):
        p = _player_dat(tmp_path, favorites=["custom_level_AAA", "custom_level_BBB"])
        assert load_favorites(p) == {"custom_level_AAA", "custom_level_BBB"}

    def test_no_players_or_corrupt_file_is_empty(self, tmp_path):
        p = tmp_path / "PlayerData.dat"
        p.write_text(json.dumps({"localPlayers": []}))
        assert load_favorites(p) == set()
        p.write_text("{bad")
        assert load_favorites(p) == set()
        assert load_favorites(tmp_path / "absent.dat") == set()


class TestLoadPlayerStats:
    def test_zero_play_count_entries_skipped(self, tmp_path):
        p = _player_dat(tmp_path, levels=[
            {"levelId": "custom_level_X", "difficulty": 3,
             "highScore": 100, "maxRank": 4, "playCount": 0},
        ])
        assert load_player_stats(p) == {}

    def test_rank_labels_and_fc(self, tmp_path):
        p = _player_dat(tmp_path, levels=[
            {"levelId": "custom_level_X", "difficulty": 4,
             "highScore": 900000, "maxRank": 6, "playCount": 7, "fullCombo": True},
        ])
        stats = load_player_stats(p)["custom_level_X"][4]
        assert (stats.score, stats.rank, stats.plays, stats.full_combo) == \
            (900000, "SS", 7, True)

    def test_played_but_zero_score_is_dnf(self, tmp_path):
        p = _player_dat(tmp_path, levels=[
            {"levelId": "custom_level_X", "difficulty": 1,
             "highScore": 0, "maxRank": 0, "playCount": 2},
        ])
        assert load_player_stats(p)["custom_level_X"][1].rank == "DNF"


class TestSongLevelIds:
    def test_hash_form_first_then_folder_forms(self):
        song = SimpleNamespace(song_hash="ABC123", folder=SimpleNamespace(name="dir"))
        assert song_level_ids(song) == [
            "custom_level_ABC123", "custom_level_dir", "dir",
        ]

    def test_no_hash_omits_hash_form(self):
        song = SimpleNamespace(song_hash="", folder=SimpleNamespace(name="dir"))
        assert song_level_ids(song) == ["custom_level_dir", "dir"]


class TestGetSongStats:
    def test_merges_hash_and_folder_entries(self):
        """Vanilla writes folder-name ids, SongCore writes hash ids — plays
        sum, the higher score wins, and FC is an OR across both."""
        song = SimpleNamespace(song_hash="ABC", folder=SimpleNamespace(name="dir"))
        all_stats = {
            "custom_level_ABC": {3: DiffStat(500, "A", 2, full_combo=False)},
            "custom_level_dir": {3: DiffStat(900, "S", 4, full_combo=True),
                                 4: DiffStat(100, "C", 1)},
        }
        merged = get_song_stats(song, all_stats)
        assert merged[3].score == 900
        assert merged[3].rank == "S"
        assert merged[3].plays == 6
        assert merged[3].full_combo is True
        assert merged[4].score == 100

    def test_no_entries_returns_none(self):
        song = SimpleNamespace(song_hash="", folder=SimpleNamespace(name="dir"))
        assert get_song_stats(song, {}) is None
