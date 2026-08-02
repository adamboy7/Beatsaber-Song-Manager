"""Configuration accessors — defaults, round trips, and hostile values.

config.json is hand-editable and survives upgrades, so every getter has to
cope with a key that is absent, a key holding the wrong type, and a key holding
a plausible-looking string. The boolean flags all use ``is True`` rather than a
truthiness test for exactly that reason: ``"false"`` is a non-empty string, and
a config that turns a feature *on* because someone typed the word "false" would
be very hard to diagnose.
"""

from __future__ import annotations

import json

import pytest

from libraries import app_config


class _ConfigFile:
    """Dict-ish view over a real config.json on disk."""

    def __init__(self, path):
        self._path = path

    def _read(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except OSError:
            return {}

    def __getitem__(self, key):
        return self._read()[key]

    def __setitem__(self, key, value):
        data = self._read()
        data[key] = value
        self._path.write_text(json.dumps(data), encoding="utf-8")

    def __contains__(self, key) -> bool:
        return key in self._read()


@pytest.fixture
def config(tmp_path, monkeypatch):
    """A real config.json in a temp dir, never the user's own.

    Patches the *path* rather than ``load_config``/``save_config`` themselves,
    so both run for real — including ``save_config``'s startup-key seeding and
    the JSON round trip that turns a written ``True`` back into a bool. A
    stubbed saver would hide the seeding entirely, which is most of what these
    tests are about.
    """
    path = tmp_path / "config.json"
    monkeypatch.setattr(app_config, "config_path", lambda: path)
    return _ConfigFile(path)


# ── Startup windows ──────────────────────────────────────────────────────────

_STARTUP_FLAGS = (
    ("open_queue_on_startup", app_config.get_open_queue_on_startup),
    ("open_visualizer_on_startup", app_config.get_open_visualizer_on_startup),
)


@pytest.mark.parametrize("key,getter", _STARTUP_FLAGS)
def test_startup_windows_are_off_when_unset(config, key, getter):
    """An absent key is the same as False.

    Configs written before these keys existed have to produce exactly the
    startup they always did, so the seeding in ``save_config`` is a
    convenience rather than something the getters depend on.
    """
    assert getter() is False
    assert key not in config


@pytest.mark.parametrize("key,getter", _STARTUP_FLAGS)
def test_a_hand_edited_true_turns_the_window_on(config, key, getter):
    """Editing the key is the only way to enable these — there's no menu."""
    config[key] = True
    assert getter() is True


@pytest.mark.parametrize("key,getter", _STARTUP_FLAGS)
@pytest.mark.parametrize("junk", ["false", "true", 0, 1, None, [], "on"])
def test_a_hand_edited_value_never_turns_a_window_on(config, key, getter, junk):
    """Only a real JSON ``true`` counts.

    ``"false"`` is the case that matters: it is a non-empty string, so a
    truthiness test would read someone's attempt to *disable* the window as a
    request to enable it. These keys are typed by hand, so quoted booleans are
    a likely mistake rather than a hypothetical one.
    """
    config[key] = junk
    assert getter() is False


def test_the_two_startup_flags_are_independent(config):
    config["open_queue_on_startup"] = True
    assert app_config.get_open_queue_on_startup() is True
    assert app_config.get_open_visualizer_on_startup() is False


@pytest.mark.parametrize("key,_getter", _STARTUP_FLAGS)
def test_there_is_no_setter_for_a_startup_flag(key, _getter):
    """The app writes the default; the user writes the choice.

    Deliberately not symmetrical with the other preferences, because these two
    have no UI. A setter with no caller is dead code, and a setter that
    acquires one means a menu item has appeared — at which point the README
    table saying "edited by hand" is wrong. Failing here is the prompt to
    update it.
    """
    stem = key.replace("_on_startup", "")
    assert not hasattr(app_config, f"set_{key}")
    assert not [n for n in dir(app_config) if n.startswith("set_") and stem in n]


# ── Seeding into a fresh config ──────────────────────────────────────────────


def test_a_fresh_config_lists_both_startup_keys(config):
    """A preference with no UI has to be visible in the file to be findable.

    ``set_custom_levels`` is what creates config.json on a first run, so
    seeding on any write is what gets these in front of a new user without a
    dedicated first-run step.
    """
    app_config.set_custom_levels(r"C:\Games\Beat Saber\CustomLevels")
    assert config["open_queue_on_startup"] is False
    assert config["open_visualizer_on_startup"] is False


def test_seeding_never_overwrites_an_existing_choice(config):
    """Someone who set these by hand must not have them reset by a volume change."""
    config["open_visualizer_on_startup"] = True
    app_config.set_volume(60)
    assert config["open_visualizer_on_startup"] is True
    assert config["open_queue_on_startup"] is False


def test_an_older_config_picks_the_keys_up_on_the_next_write(config):
    """A seed, not a migration — no version number, nothing to run once."""
    config["custom_levels"] = "/somewhere"
    assert "open_queue_on_startup" not in config
    app_config.set_volume(20)
    assert config["open_queue_on_startup"] is False
    assert config["custom_levels"] == "/somewhere"


def test_saving_does_not_mutate_the_callers_dict(config):
    """``save_config`` takes a dict callers built from ``load_config``.

    ``_save_playback_config`` reuses its local afterwards, so seeding has to
    copy rather than write through.
    """
    cfg = {"volume": 30}
    app_config.save_config(cfg)
    assert cfg == {"volume": 30}


def test_the_seeded_keys_are_the_ones_the_getters_read():
    """One list, so a rename can't leave the seed pointing at a dead key."""
    assert app_config.STARTUP_WINDOW_KEYS == (
        "open_queue_on_startup", "open_visualizer_on_startup",
    )
    assert [key for key, _ in _STARTUP_FLAGS] == list(
        app_config.STARTUP_WINDOW_KEYS
    )


# ── Volume ───────────────────────────────────────────────────────────────────


def test_volume_defaults_when_unset(config):
    assert app_config.get_volume() == app_config.DEFAULT_VOLUME


@pytest.mark.parametrize("stored,expected", [(-10, 0), (0, 0), (55, 55),
                                             (100, 100), (250, 100)])
def test_volume_is_clamped_on_read(config, stored, expected):
    config["volume"] = stored
    assert app_config.get_volume() == expected


def test_volume_is_clamped_on_write(config):
    app_config.set_volume(9000)
    assert config["volume"] == 100


def test_a_junk_volume_falls_back_to_the_default(config):
    config["volume"] = "loud"
    assert app_config.get_volume() == app_config.DEFAULT_VOLUME


# ── Split preview ────────────────────────────────────────────────────────────


def test_split_preview_is_off_by_default(config):
    assert app_config.get_split_preview() is False


def test_split_preview_round_trips(config):
    assert app_config.set_split_preview(True)
    assert app_config.get_split_preview() is True


def test_a_hand_edited_split_preview_value_is_ignored(config):
    config["cinema_split_preview"] = "false"
    assert app_config.get_split_preview() is False


# ── Cinema video quality ─────────────────────────────────────────────────────


def test_video_quality_defaults_to_720(config):
    assert app_config.get_video_quality() == "720"


@pytest.mark.parametrize("quality", app_config.VIDEO_QUALITIES)
def test_every_documented_quality_round_trips(config, quality):
    assert app_config.set_video_quality(quality)
    assert app_config.get_video_quality() == quality


def test_an_unknown_quality_is_refused_rather_than_stored(config):
    assert not app_config.set_video_quality("4k")
    assert app_config.get_video_quality() == app_config.DEFAULT_VIDEO_QUALITY


@pytest.mark.parametrize("stored", ["4k", "", None, [], "1080p"])
def test_a_hand_edited_quality_falls_back_to_the_default(config, stored):
    config[app_config.VIDEO_QUALITY_KEY] = stored
    assert app_config.get_video_quality() == app_config.DEFAULT_VIDEO_QUALITY


def test_an_unquoted_number_is_read_as_the_quality_meant(config):
    """1080 without quotes is a JSON int, and obviously means "1080"."""
    config[app_config.VIDEO_QUALITY_KEY] = 1080
    assert app_config.get_video_quality() == "1080"


def test_the_quality_key_is_seeded_so_it_can_be_discovered(config, tmp_path):
    app_config.set_custom_levels(tmp_path)
    assert config[app_config.VIDEO_QUALITY_KEY] == app_config.DEFAULT_VIDEO_QUALITY


def test_seeding_never_overwrites_a_chosen_quality(config, tmp_path):
    app_config.set_video_quality("max")
    app_config.set_custom_levels(tmp_path)
    assert config[app_config.VIDEO_QUALITY_KEY] == "max"


# ── Format selector ──────────────────────────────────────────────────────────


def test_the_default_selector_is_the_720p_format_cinema_uses(config):
    assert app_config.video_format_selector() == (
        "bestvideo[height<=720][vcodec*=avc1]+bestaudio[acodec*=mp4]"
    )


def test_the_default_selector_has_no_fallback_rungs(config):
    """720p must stay byte-identical to what the mod itself downloads."""
    assert "/" not in app_config.video_format_selector("720")


def test_1080_still_ends_at_the_720p_pair(config):
    """A video with only low-resolution streams downloads rather than fails."""
    assert app_config.video_format_selector("1080").endswith(
        "/bestvideo[height<=720][vcodec*=avc1]+bestaudio[acodec*=mp4]"
    )


@pytest.mark.parametrize("quality", app_config.VIDEO_QUALITIES)
def test_no_rung_can_land_on_av1(config, quality):
    """AV1 ships in .mp4, so --recode-video mp4 skips it and Cinema — whose
    Unity player cannot decode AV1 — shows a black screen behind the map."""
    for rung in app_config.video_format_selector(quality).split("/"):
        assert "avc1" in rung or "vcodec!*=av01" in rung, rung


@pytest.mark.parametrize("quality", ["720", "1080"])
def test_a_capped_quality_only_ever_asks_for_h264(config, quality):
    """Neither capped setting may re-encode, so every rung must be avc1."""
    rungs = app_config.video_format_selector(quality).split("/")
    assert all("vcodec*=avc1" in rung for rung in rungs)


@pytest.mark.parametrize("quality", ["720", "1080"])
def test_a_capped_quality_does_not_sort(config, quality):
    assert app_config.video_sort_order(quality) is None


def test_max_does_not_constrain_the_codec_to_h264(config):
    """It sorts toward avc1 instead — a constraint would cap it at 1080p."""
    assert "vcodec*=avc1" not in app_config.video_format_selector("max")


def test_max_sorts_resolution_above_codec(config):
    """Resolution first is what lets 4K win; codec only breaks the tie."""
    order = app_config.video_sort_order("max")
    assert order.index("res") < order.index("vcodec")


def test_max_breaks_a_resolution_tie_toward_h264(config):
    """At 1080p both codecs exist, and picking avc1 is what avoids a
    pointless transcode of a stream Cinema could already play."""
    assert "vcodec:avc1" in app_config.video_sort_order("max")


def test_max_caps_4k_to_30fps_and_everything_else_to_1440p(config):
    """Not a quality judgement — an H.264 level 5.1 limit. 4K60 transcodes to
    5.2, past what Media Foundation decodes. See test_cinema_video_quality.py
    for the arithmetic and the end-to-end selection."""
    rungs = app_config.video_format_selector("max").split("/")
    assert rungs[0].startswith("bestvideo[vcodec!*=av01][height<=2160][fps<=30]")
    assert all("height<=1440" in rung for rung in rungs[1:])


@pytest.mark.parametrize("quality", app_config.VIDEO_QUALITIES)
def test_every_first_rung_pairs_with_mp4_audio(config, quality):
    """An avc1 video merged with an Opus track lands in .mkv and gets
    re-encoded despite the video having been H.264 the whole time."""
    first = app_config.video_format_selector(quality).split("/")[0]
    assert "bestaudio[acodec*=mp4]" in first


def test_the_selector_follows_the_stored_quality(config):
    app_config.set_video_quality("1080")
    assert app_config.video_format_selector() == app_config.video_format_selector("1080")


# ── Download arguments ───────────────────────────────────────────────────────


def test_the_default_download_args_are_just_a_format(config):
    assert app_config.video_download_args("720") == [
        "-f", "bestvideo[height<=720][vcodec*=avc1]+bestaudio[acodec*=mp4]"
    ]


def test_max_download_args_carry_the_sort(config):
    args = app_config.video_download_args("max")
    assert args[0] == "-f"
    assert args[2] == "-S"
    assert args[3] == app_config.video_sort_order("max")


def test_download_args_follow_the_stored_quality(config):
    app_config.set_video_quality("max")
    assert app_config.video_download_args() == app_config.video_download_args("max")
