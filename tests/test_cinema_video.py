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


# ── YouTube URL parsing ──────────────────────────────────────────────────────

ID = "dQw4w9WgXcQ"


@pytest.mark.parametrize("url", [
    f"https://www.youtube.com/watch?v={ID}",
    f"http://youtube.com/watch?v={ID}",
    f"https://m.youtube.com/watch?v={ID}",
    f"https://music.youtube.com/watch?v={ID}",
    f"www.youtube.com/watch?v={ID}",            # no scheme, as pasted
    f"youtube.com/watch?v={ID}",
    f"https://youtu.be/{ID}",
    f"youtu.be/{ID}",
    f"https://www.youtube.com/shorts/{ID}",
    f"https://www.youtube.com/embed/{ID}",
    f"https://www.youtube-nocookie.com/embed/{ID}",
    f"https://www.youtube.com/live/{ID}",
    f"https://www.youtube.com/v/{ID}",
    ID,                                          # bare ID
    f"  {ID}  ",                                 # stray whitespace
])
def test_parse_youtube_url_accepts_every_form(url):
    assert cinema_video.parse_youtube_url(url) == (ID, None)


def test_parse_youtube_url_ignores_playlist_parameters():
    # A link copied from a playlist still points at one video; list= and
    # index= describe the surrounding queue, which is not our business.
    assert cinema_video.parse_youtube_url(
        f"https://www.youtube.com/watch?v={ID}&list=PLabc123&index=4"
    ) == (ID, None)


@pytest.mark.parametrize("url,expected", [
    (f"https://www.youtube.com/watch?v={ID}&t=42", 42),
    (f"https://youtu.be/{ID}?t=42s", 42),
    (f"https://youtu.be/{ID}?t=1m30s", 90),
    (f"https://www.youtube.com/watch?v={ID}&t=1h2m3s", 3723),
    (f"https://www.youtube.com/watch?v={ID}&t=2m", 120),
    (f"https://www.youtube.com/embed/{ID}?start=90", 90),
    (f"https://www.youtube.com/watch?v={ID}#t=15", 15),
    (f"https://www.youtube.com/watch?v={ID}&t=0", 0),
])
def test_parse_youtube_url_reads_the_timestamp(url, expected):
    assert cinema_video.parse_youtube_url(url) == (ID, expected)


@pytest.mark.parametrize("url", [
    f"https://www.youtube.com/watch?v={ID}&t=",
    f"https://www.youtube.com/watch?v={ID}&t=later",
    f"https://www.youtube.com/watch?v={ID}&t=1:30",   # not a form YouTube emits
])
def test_parse_youtube_url_ignores_an_unreadable_timestamp(url):
    # An unparseable t= must not sink the whole link — the video ID is the
    # part that matters and offset 0 is the safe default.
    assert cinema_video.parse_youtube_url(url) == (ID, None)


@pytest.mark.parametrize("junk", [
    "", "   ", None,
    "not a url",
    "https://vimeo.com/123456789",
    "https://example.com/watch?v=" + ID,          # right shape, wrong host
    "https://www.youtube.com/watch?v=tooshort",   # IDs are exactly 11 chars
    "https://www.youtube.com/watch?v=" + ID + "extra",
    "https://www.youtube.com/",                   # no video at all
    "https://www.youtube.com/@somechannel",
    "https://www.youtube.com/playlist?list=PLabc",
    "abcdefghij!",                                # 11 chars, illegal one
])
def test_parse_youtube_url_rejects_junk(junk):
    assert cinema_video.parse_youtube_url(junk) == (None, None)


# ── Filename derivation (must match VideoConfig.GetVideoFileName) ────────────

def test_derive_video_filename_replaces_illegal_chars(tmp_path):
    assert cinema_video.derive_video_filename(
        tmp_path, "DECO*27 - Ghost Rule feat. Hatsune Miku"
    ) == "DECO_27 - Ghost Rule feat_ Hatsune Miku.mp4"


def test_derive_video_filename_strips_emoji(tmp_path):
    # Util.FilterEmoji drops \p{Cs} — UTF-16 surrogates — so in Python terms
    # every character above the BMP goes. Leaving one in derives a name the
    # mod will never look for, and it re-downloads the video on first play.
    assert cinema_video.derive_video_filename(
        tmp_path, "Song 🎵 Title 🔥"
    ) == "Song  Title .mp4"


def test_derive_video_filename_keeps_non_astral_characters(tmp_path):
    # Japanese titles are ordinary BMP text and must survive untouched;
    # a too-eager "strip non-ASCII" would mangle a large slice of the library.
    assert cinema_video.derive_video_filename(
        tmp_path, "初音ミク - メルト"
    ) == "初音ミク - メルト.mp4"


def test_derive_video_filename_truncates_to_max_path(tmp_path):
    name = cinema_video.derive_video_filename(tmp_path, "A" * 400)
    assert len(str(tmp_path)) + len(name) + len(".fxxxx") <= 259
    assert name.endswith(".mp4")


def test_derive_video_filename_strips_emoji_before_truncating(tmp_path):
    # Order matters: truncating first can cut mid-astral-character. Once the
    # emoji are gone there is no surrogate left to split.
    name = cinema_video.derive_video_filename(tmp_path, "🎵" * 200 + "B" * 200)
    assert "🎵" not in name


def test_derive_video_filename_appends_exactly_one_extension(tmp_path):
    # A title that already reads like a filename still gains ".mp4": the
    # period in it was replaced with "_" first, so the mod's EndsWith(".mp4")
    # check doesn't fire either. Matching that is the whole point.
    assert cinema_video.derive_video_filename(
        tmp_path, "My Video.mp4"
    ) == "My Video_mp4.mp4"


def test_derive_video_filename_falls_back_to_the_video_id(tmp_path):
    assert cinema_video.derive_video_filename(tmp_path, "", ID) == f"{ID}.mp4"


def test_song_data_reads_back_the_name_create_config_writes(tmp_path):
    # The reader and the writer are the two halves that have to agree; if
    # they ever drift, the app downloads a video it then can't find.
    from libraries.song_data import SongInfo

    title = "初音ミク 🎵 DECO*27 - Ghost Rule feat. Miku"
    folder = tmp_path / "12345 (Ghost Rule - DECO27)"
    folder.mkdir()
    (folder / "Info.dat").write_text(json.dumps({"_songName": "Ghost Rule"}))
    cinema_video.create_config(folder, ID, title, "DECO*27", 212)

    expected = cinema_video.derive_video_filename(folder, title, ID)
    (folder / expected).write_bytes(b"not really an mp4")

    song = SongInfo(folder)
    assert song.cinema_video_file == expected
    assert song.has_playable_cinema_video


# ── create_config ────────────────────────────────────────────────────────────

def test_create_config_writes_the_six_fields_cinema_sets(tmp_path):
    path = cinema_video.create_config(
        tmp_path, ID, "Some Video", "Some Channel", 212,
    )
    assert path.name == "cinema-video.json"
    assert read_config(path) == {
        "videoID": ID,
        "title": "Some Video",
        "author": "Some Channel",
        "videoFile": "Some Video.mp4",
        "duration": 212,
        "offset": 0,
    }


def test_create_config_never_claims_to_be_a_mapper_config(tmp_path):
    # configByMapper gates save_offset's originalOffset bookkeeping. Setting
    # it on a config the user created would make Cinema offer to "reset" the
    # offset to a value we invented.
    path = cinema_video.create_config(tmp_path, ID, "T", "A", 10)
    data = read_config(path)
    assert "configByMapper" not in data
    assert "userSettings" not in data


def test_create_config_seeds_the_offset_from_a_timestamp(tmp_path):
    path = cinema_video.create_config(
        tmp_path, ID, "T", "A", 10, offset_ms=42_000,
    )
    assert read_config(path)["offset"] == 42_000


def test_create_config_accepts_an_explicit_video_file(tmp_path):
    # The download happens before the manifest is written, so the caller
    # passes the name it actually downloaded to rather than re-deriving it.
    path = cinema_video.create_config(
        tmp_path, ID, "T", "A", 10, video_file="downloaded as this.mp4",
    )
    assert read_config(path)["videoFile"] == "downloaded as this.mp4"


def test_create_config_clamps_the_offset_and_floors_the_duration(tmp_path):
    path = cinema_video.create_config(
        tmp_path, ID, "T", "A", -5, offset_ms=10**12,
    )
    data = read_config(path)
    assert data["offset"] == 2_147_483_647
    assert data["duration"] == 0


def test_create_config_tolerates_a_missing_duration(tmp_path):
    # Livestreams and some music-service uploads have no duration in
    # --dump-json; song_data tolerates 0 and the editor probes the file.
    path = cinema_video.create_config(tmp_path, ID, "T", "A", None)
    assert read_config(path)["duration"] == 0


def test_create_config_backs_up_an_existing_config(tmp_path):
    write_config(tmp_path, BASE)
    cinema_video.create_config(tmp_path, ID, "New Title", "New Author", 99)
    bak = tmp_path / "cinema-video.json.bak"
    assert read_config(bak) == BASE
    assert read_config(tmp_path / "cinema-video.json")["title"] == "New Title"


def test_create_config_keeps_the_first_backup(tmp_path):
    write_config(tmp_path, BASE)
    cinema_video.create_config(tmp_path, ID, "First", "A", 1)
    cinema_video.create_config(tmp_path, ID, "Second", "A", 1)
    bak = tmp_path / "cinema-video.json.bak"
    assert read_config(bak)["title"] == BASE["title"]


def test_create_config_refuses_the_legacy_list_format(tmp_path):
    (tmp_path / "cinema-video.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        cinema_video.create_config(tmp_path, ID, "T", "A", 10)
    assert (tmp_path / "cinema-video.json").read_text(encoding="utf-8") == "[]"


def test_create_config_writes_to_the_spelling_that_exists(tmp_path):
    write_config(tmp_path, BASE, name="Cinema-Video.json")
    path = cinema_video.create_config(tmp_path, ID, "T", "A", 10)
    assert path.name == "Cinema-Video.json"
    assert not (tmp_path / "cinema-video.json").exists()


def test_create_config_keeps_non_ascii_titles_readable(tmp_path):
    path = cinema_video.create_config(tmp_path, ID, "初音ミク", "A", 10)
    assert "初音ミク" in path.read_text(encoding="utf-8")


def test_create_config_output_round_trips_through_save_offset(tmp_path):
    # The editor is the next step after every create, so the file this
    # produces has to be one save_offset can read and rewrite.
    cinema_video.create_config(tmp_path, ID, "T", "A", 10, offset_ms=42_000)
    cinema_video.save_offset(tmp_path, 41_500)
    data = read_config(tmp_path / "cinema-video.json")
    assert data["offset"] == 41_500
    assert "userSettings" not in data  # not a mapper config, nothing to reset


def test_original_offset_ms_helper():
    assert cinema_video.original_offset_ms({}) is None
    assert cinema_video.original_offset_ms({"userSettings": {}}) is None
    assert cinema_video.original_offset_ms(
        {"userSettings": {"customOffset": False, "originalOffset": 10}}
    ) is None
    assert cinema_video.original_offset_ms(
        {"userSettings": {"customOffset": True, "originalOffset": 10}}
    ) == 10
