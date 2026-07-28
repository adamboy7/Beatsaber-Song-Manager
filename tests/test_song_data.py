"""song_data.py — Info.dat parsing, cinema manifests, custom tags, hashing."""

from __future__ import annotations

import hashlib
import json

from conftest import make_song_folder, v2_info

from libraries.song_data import (
    SongInfo,
    _cinema_safe_filename,
    compute_song_hash,
    load_songs,
    save_custom_tags,
)


# ── v2 parsing ───────────────────────────────────────────────────────────────

class TestV2Parsing:
    def test_basic_fields(self, song_factory):
        _, song = song_factory(
            title="Freedom Dive", sub="[FOUR DIMENSIONS]", artist="xi",
            mapper="Psi", bpm=222.22,
        )
        assert song.song_name == "Freedom Dive"
        assert song.author == "xi"
        assert song.mapper == "Psi"
        assert song.bpm == 222.22
        assert song.display_name == "Freedom Dive [FOUR DIMENSIONS]"
        assert song.song_id == "abc1"

    def test_cover_and_audio_paths_only_when_present(self, tmp_path):
        info = v2_info(cover="missing.jpg", audio="song.egg")
        folder = make_song_folder(tmp_path, "x (A - B)", info)
        (folder / "missing.jpg").unlink()
        song = SongInfo(folder)
        assert song.cover_path is None
        assert song.audio_path == folder / "song.egg"

    def test_difficulties_and_labels(self, song_factory):
        _, song = song_factory(diffs=[
            ("Easy", None),
            ("ExpertPlus", {"_difficultyLabel": "Vibro"}),
        ])
        assert song.difficulties == {0, 4}
        assert song.diff_labels == {4: "Vibro"}

    def test_standard_label_wins_over_other_characteristic(self, song_factory):
        _, song = song_factory(diffs=[
            ("Lawless", "Expert", {"_difficultyLabel": "Chaos"}),
            ("Standard", "Expert", {"_difficultyLabel": "Clean"}),
        ])
        assert song.diff_labels[3] == "Clean"

    def test_mod_requirements_and_suggestions(self, song_factory):
        _, song = song_factory(diffs=[
            ("Expert", {"_requirements": ["Chroma", "none"],
                        "_suggestions": ["Cinema"]}),
        ])
        assert song.mod_required == frozenset({"Chroma"})
        assert song.mod_suggested == frozenset({"Cinema"})
        assert song.requires_chroma
        assert not song.requires_noodle
        assert song.has_cinema

    def test_unkeyed_folder_name(self, song_factory):
        folder, song = song_factory(folder_name="My Custom Song")
        assert song.song_id == ""
        assert song.display_name == "Test Song"  # Info.dat still parsed

    def test_broken_info_dat_falls_back_to_folder_name(self, tmp_path):
        folder = tmp_path / "broken (X - Y)"
        folder.mkdir()
        (folder / "Info.dat").write_text("{not json", encoding="utf-8")
        song = SongInfo(folder)
        assert song.display_name == "broken (X - Y)"
        assert song.song_id == "broken"

    def test_search_blob_covers_all_search_fields(self, song_factory):
        _, song = song_factory(title="AaA", artist="BbB", mapper="CcC")
        for needle in ("aaa", "bbb", "ccc", "abc1"):
            assert needle in song.search_blob


# ── v4 parsing ───────────────────────────────────────────────────────────────

class TestV4Parsing:
    def _v4_info(self):
        return {
            "version": "4.0.0",
            "song": {"title": "V4 Song", "subTitle": "sub", "author": "V4 Artist"},
            "audio": {"bpm": 180, "songFilename": "song.ogg"},
            "coverImageFilename": "cover.png",
            "difficultyBeatmaps": [
                {
                    "characteristic": "Standard", "difficulty": "Expert",
                    "beatmapDataFilename": "Expert.dat",
                    "lightshowDataFilename": "Lightshow.dat",
                    "beatmapAuthors": {"mappers": ["MapperA", "MapperB"]},
                },
                {
                    "characteristic": "Standard", "difficulty": "ExpertPlus",
                    "beatmapDataFilename": "ExpertPlus.dat",
                    "beatmapAuthors": {"mappers": ["MapperA"]},
                },
            ],
        }

    def test_v4_fields(self, tmp_path):
        folder = make_song_folder(tmp_path, "v4f (V4 Song - MapperA)", self._v4_info())
        song = SongInfo(folder)
        assert song.song_name == "V4 Song"
        assert song.author == "V4 Artist"
        assert song.bpm == 180.0
        assert song.difficulties == {3, 4}
        assert song.mapper == "MapperA, MapperB"  # deduped, first-seen order


# ── Cinema manifests ─────────────────────────────────────────────────────────

class TestCinema:
    def test_safe_filename_replaces_illegal_chars_and_periods(self):
        assert (
            _cinema_safe_filename("DECO*27 - Ghost Rule feat. Hatsune Miku")
            == "DECO_27 - Ghost Rule feat_ Hatsune Miku"
        )

    def test_manifest_without_video_file_derives_from_title(self, song_factory):
        folder, _ = song_factory()
        (folder / "cinema-video.json").write_text(json.dumps({
            "videoID": "dQw4w9WgXcQ", "title": "Some: Video.Title",
            "offset": -300, "duration": 212,
        }))
        song = SongInfo(folder)
        assert song.has_cinema_video
        assert song.cinema_video_file == "Some_ Video_Title.mp4"
        assert song.cinema_video_path is None  # not downloaded
        assert not song.has_playable_cinema_video
        assert song.cinema_video_offset_ms == -300
        assert song.cinema_video_duration_s == 212

    def test_video_path_set_when_file_downloaded(self, song_factory):
        folder, _ = song_factory()
        (folder / "cinema-video.json").write_text(json.dumps({
            "videoID": "abc", "videoFile": "clip.mp4",
        }))
        (folder / "clip.mp4").write_bytes(b"\x00")
        song = SongInfo(folder)
        assert song.cinema_video_path == folder / "clip.mp4"
        assert song.has_playable_cinema_video
        assert song.has_cinema  # manifest alone implies cinema support

    def test_manifest_case_insensitive_and_bad_offsets_coerced(self, song_factory):
        folder, _ = song_factory()
        (folder / "Cinema-Video.json").write_text(json.dumps({
            "videoID": "abc", "offset": "garbage", "duration": None,
        }))
        song = SongInfo(folder)
        assert song.has_cinema_video
        assert song.cinema_video_file == "abc.mp4"  # falls back to videoID
        assert song.cinema_video_offset_ms == 0
        assert song.cinema_video_duration_s == 0


# ── Custom tags ──────────────────────────────────────────────────────────────

class TestCustomTags:
    def test_round_trip(self, song_factory):
        folder, _ = song_factory()
        save_custom_tags(folder, {"speed", "tech"})
        song = SongInfo(folder)
        assert song.custom_tags == frozenset({"speed", "tech"})

    def test_corrupt_tags_file_is_empty_set(self, song_factory):
        folder, _ = song_factory()
        (folder / "tags.json").write_text("{oops", encoding="utf-8")
        song = SongInfo(folder)
        assert song.custom_tags == frozenset()


# ── Hashing ──────────────────────────────────────────────────────────────────

class TestComputeSongHash:
    def test_v2_hash_is_sha1_of_info_plus_diffs_in_order(self, song_factory):
        folder, _ = song_factory(diffs=[("Easy", None), ("Expert", None)])
        expected = hashlib.sha1(
            (folder / "Info.dat").read_bytes()
            + (folder / "EasyStandard.dat").read_bytes()
            + (folder / "ExpertStandard.dat").read_bytes()
        ).hexdigest().upper()
        assert compute_song_hash(folder) == expected

    def test_v4_hash_includes_lightshow_files(self, tmp_path):
        info = {
            "version": "4.0.0",
            "song": {"title": "T"}, "audio": {"bpm": 100},
            "difficultyBeatmaps": [{
                "characteristic": "Standard", "difficulty": "Expert",
                "beatmapDataFilename": "Expert.dat",
                "lightshowDataFilename": "Lightshow.dat",
            }],
        }
        folder = make_song_folder(tmp_path, "v4h (T - M)", info)
        expected = hashlib.sha1(
            (folder / "Info.dat").read_bytes()
            + (folder / "Expert.dat").read_bytes()
            + (folder / "Lightshow.dat").read_bytes()
        ).hexdigest().upper()
        assert compute_song_hash(folder) == expected

    def test_missing_referenced_diff_returns_empty(self, song_factory):
        folder, _ = song_factory(diffs=[("Expert", None)])
        (folder / "ExpertStandard.dat").unlink()
        assert compute_song_hash(folder) == ""

    def test_no_info_dat_returns_empty(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert compute_song_hash(empty) == ""

    def test_info_file_override(self, song_factory):
        """A .bak Info.dat can be hashed in place of the live one."""
        folder, _ = song_factory(diffs=[("Expert", None)])
        original = compute_song_hash(folder)
        (folder / "Info.dat.bak").write_bytes((folder / "Info.dat").read_bytes())
        # Mutate the live Info.dat; hashing the .bak must still give the original.
        live = json.loads((folder / "Info.dat").read_text())
        live["_songName"] = "Edited"
        (folder / "Info.dat").write_text(json.dumps(live))
        assert compute_song_hash(folder, folder / "Info.dat.bak") == original
        assert compute_song_hash(folder) != original


# ── load_songs ───────────────────────────────────────────────────────────────

class TestLoadSongs:
    def test_skips_files_and_folders_without_info_dat(self, tmp_path):
        make_song_folder(tmp_path, "a (S - M)", v2_info(title="A"))
        (tmp_path / "not_a_song").mkdir()
        (tmp_path / "stray.txt").write_text("x")
        songs = load_songs(tmp_path)
        assert [s.song_name for s in songs] == ["A"]

    def test_case_insensitive_info_dat(self, tmp_path):
        folder = make_song_folder(tmp_path, "b (S - M)", v2_info(title="B"))
        (folder / "Info.dat").rename(folder / "info.dat")
        songs = load_songs(tmp_path)
        assert [s.song_name for s in songs] == ["B"]
