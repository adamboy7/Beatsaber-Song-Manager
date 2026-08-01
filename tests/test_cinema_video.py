"""cinema_video — the only writer of cinema-video.json.

The mod reads far more fields than we parse, so the round-trip guarantees
matter more than the offset write itself: dropping a key here silently breaks
the map in-game. The ``userSettings`` behaviour mirrors ``CustomizeOffset`` in
the mod's own ``BeatSaberCinema/VideoMenu/VideoMenu.cs``
(https://github.com/Kevga/BeatSaberCinema).
"""

from __future__ import annotations

import json

import pytest

from libraries import cinema_video


def write_config(folder, data: dict, name: str = "cinema-video.json"):
    path = folder / name
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def read_config(path):
    return json.loads(path.read_text(encoding="utf-8"))


BASE = {
    "videoID": "dQw4w9WgXcQ",
    "title": "Some Video",
    "author": "Some Channel",
    "videoFile": "Some Video.mp4",
    "duration": 212,
    "offset": -1200,
}


# ── Reading ──────────────────────────────────────────────────────────────────

def test_find_config_matches_alternate_spellings(tmp_path):
    write_config(tmp_path, BASE, name="Cinema-Video.json")
    assert cinema_video.find_config(tmp_path).name == "Cinema-Video.json"


def test_find_config_returns_none_when_absent(tmp_path):
    assert cinema_video.find_config(tmp_path) is None


def test_load_config_rejects_the_legacy_list_format(tmp_path):
    # Pre-1.0 Cinema stored a JSON array of videos; rewriting one as an
    # object would produce a file the mod can't read.
    (tmp_path / "cinema-video.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        cinema_video.load_config(tmp_path)


def test_read_offset_ms_survives_a_junk_value(tmp_path):
    write_config(tmp_path, {**BASE, "offset": "nonsense"})
    assert cinema_video.read_offset_ms(tmp_path) == 0


def test_read_offset_ms_on_missing_config(tmp_path):
    assert cinema_video.read_offset_ms(tmp_path) == 0


# ── Writing ──────────────────────────────────────────────────────────────────

def test_save_offset_writes_the_value(tmp_path):
    path = write_config(tmp_path, BASE)
    cinema_video.save_offset(tmp_path, 340)
    assert read_config(path)["offset"] == 340


def test_save_offset_preserves_unknown_keys(tmp_path):
    extras = {
        "screenPosition": {"x": 0, "y": 12.4, "z": 100},
        "colorCorrection": {"saturation": 1.1, "gamma": 1.05},
        "environment": [{"name": "Rain", "active": False}],
        "bloom": 1.3,
        "somethingCinemaAddedLater": True,
    }
    path = write_config(tmp_path, {**BASE, **extras})
    cinema_video.save_offset(tmp_path, 999)
    result = read_config(path)
    for key, value in extras.items():
        assert result[key] == value
    assert result["videoID"] == BASE["videoID"]
    assert result["duration"] == BASE["duration"]


def test_save_offset_backs_up_once(tmp_path):
    path = write_config(tmp_path, BASE)
    bak = tmp_path / "cinema-video.json.bak"

    cinema_video.save_offset(tmp_path, 100)
    assert read_config(bak)["offset"] == -1200

    # A second edit must not overwrite the backup — the .bak has to keep
    # pointing at the pre-edit original for Restore Files to be meaningful.
    cinema_video.save_offset(tmp_path, 200)
    assert read_config(bak)["offset"] == -1200
    assert read_config(path)["offset"] == 200


def test_save_offset_is_a_noop_when_unchanged(tmp_path):
    write_config(tmp_path, BASE)
    cinema_video.save_offset(tmp_path, BASE["offset"])
    assert not (tmp_path / "cinema-video.json.bak").exists()


def test_save_offset_writes_to_the_spelling_that_exists(tmp_path):
    path = write_config(tmp_path, BASE, name="Cinema-Video.json")
    cinema_video.save_offset(tmp_path, 42)
    assert read_config(path)["offset"] == 42
    assert not (tmp_path / "cinema-video.json").exists()


def test_save_offset_missing_config_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        cinema_video.save_offset(tmp_path, 42)


def test_save_offset_clamps_to_int32(tmp_path):
    path = write_config(tmp_path, BASE)
    cinema_video.save_offset(tmp_path, 10**12)
    assert read_config(path)["offset"] == 2_147_483_647


def test_save_offset_keeps_non_ascii_titles_readable(tmp_path):
    path = write_config(tmp_path, {**BASE, "title": "初音ミク"})
    cinema_video.save_offset(tmp_path, 5)
    assert "初音ミク" in path.read_text(encoding="utf-8")


# ── userSettings, per the mod's VideoMenu.cs CustomizeOffset ─────────────────

def test_mapper_config_records_the_original_offset(tmp_path):
    path = write_config(tmp_path, {**BASE, "configByMapper": True})
    cinema_video.save_offset(tmp_path, 500)
    settings = read_config(path)["userSettings"]
    assert settings == {"customOffset": True, "originalOffset": -1200}


def test_original_offset_is_written_once_not_updated(tmp_path):
    path = write_config(tmp_path, {**BASE, "configByMapper": True})
    cinema_video.save_offset(tmp_path, 500)
    cinema_video.save_offset(tmp_path, 900)
    settings = read_config(path)["userSettings"]
    assert settings["originalOffset"] == -1200  # still the mapper's value


def test_non_mapper_config_gets_no_user_settings(tmp_path):
    # A config the user searched for themselves isn't "official", so Cinema
    # never asks them to opt in to customising it.
    path = write_config(tmp_path, BASE)
    cinema_video.save_offset(tmp_path, 500)
    assert "userSettings" not in read_config(path)


def test_existing_user_settings_are_left_alone(tmp_path):
    path = write_config(tmp_path, {
        **BASE,
        "configByMapper": True,
        "userSettings": {"customOffset": True, "originalOffset": -3000},
    })
    cinema_video.save_offset(tmp_path, 500)
    assert read_config(path)["userSettings"]["originalOffset"] == -3000


# ── videoFile repair ─────────────────────────────────────────────────────────

def test_save_video_file_rewrites_an_impossible_name(tmp_path):
    # 22e58: the mapper pasted the raw video title, slash included, so Cinema
    # downloads into a subfolder and then reports the map as not downloaded.
    path = write_config(tmp_path, {
        **BASE, "videoFile": "徳川カップヌードル禁止令 / 草薙寧々.mp4",
        "configByMapper": True,
    })
    cinema_video.save_video_file(tmp_path, "徳川カップヌードル禁止令 _ 草薙寧々.mp4")
    data = read_config(path)
    assert data["videoFile"] == "徳川カップヌードル禁止令 _ 草薙寧々.mp4"
    # Everything else survives, and this isn't an offset override.
    assert data["offset"] == -1200
    assert data["videoID"] == "dQw4w9WgXcQ"
    assert "userSettings" not in data


def test_save_video_file_backs_up_once(tmp_path):
    path = write_config(tmp_path, BASE)
    cinema_video.save_video_file(tmp_path, "first.mp4")
    bak = path.with_name(path.name + ".bak")
    assert read_config(bak)["videoFile"] == "Some Video.mp4"
    cinema_video.save_video_file(tmp_path, "second.mp4")
    assert read_config(bak)["videoFile"] == "Some Video.mp4"  # still the original


def test_save_video_file_skips_a_no_op_write(tmp_path):
    path = write_config(tmp_path, BASE)
    cinema_video.save_video_file(tmp_path, "Some Video.mp4")
    assert not path.with_name(path.name + ".bak").exists()


def test_original_offset_ms_helper():
    assert cinema_video.original_offset_ms({}) is None
    assert cinema_video.original_offset_ms({"userSettings": {}}) is None
    assert cinema_video.original_offset_ms(
        {"userSettings": {"customOffset": False, "originalOffset": 10}}
    ) is None
    assert cinema_video.original_offset_ms(
        {"userSettings": {"customOffset": True, "originalOffset": 10}}
    ) == 10
