"""browser_pagination — tag parsing, validation, and filter_songs semantics.

These are the exact functions the headless CLI (--randomAdd filters) and the
GUI search box share, so regressions here break both.
"""

from __future__ import annotations

import json

import pytest

from libraries.browser_pagination import (
    TAG_SPECS,
    _has_invalid_tags,
    _KNOWN_TAGS,
    _parse_tags,
    _YN_TAGS,
    filter_songs,
)
from libraries.player_data import DiffStat
from libraries.song_data import SongInfo


# ── Tag parsing ──────────────────────────────────────────────────────────────

class TestParseTags:
    def test_plain_only(self):
        tags, plain = _parse_tags("freedom dive")
        assert tags == []
        assert plain == "freedom dive"

    def test_tags_and_plain_mix(self):
        tags, plain = _parse_tags("{mapper}:Psi ghost {bpm}:>=150")
        assert tags == [("mapper", "psi"), ("bpm", ">=150")]
        assert plain == "ghost"

    def test_quoted_value_keeps_spaces(self):
        tags, plain = _parse_tags('{artist}:"daft punk"')
        assert tags == [("artist", "daft punk")]
        assert plain == ""

    def test_tag_names_case_insensitive(self):
        tags, _ = _parse_tags("{MAPPER}:Psi")
        assert tags == [("mapper", "psi")]

    def test_empty_value_is_not_a_tag(self):
        tags, plain = _parse_tags("{difficulty}:")
        assert tags == []
        assert plain == "{difficulty}:"


class TestValidation:
    @pytest.mark.parametrize("query", [
        "{bogus}:x",
        "{favorite}:maybe",
        "{bpm}:fast",
        "{fc}:yes",
    ])
    def test_invalid(self, query):
        tags, _ = _parse_tags(query)
        assert _has_invalid_tags(tags)

    @pytest.mark.parametrize("query", [
        "{bpm}:<=200", "{bpm}:=120.5", "{favorite}:y", "{difficulty}:expertplus",
        "{custom}:speed", "{cinema}:n", "{fc}:n",
    ])
    def test_valid(self, query):
        tags, _ = _parse_tags(query)
        assert not _has_invalid_tags(tags)


class TestTagSpecs:
    """TAG_SPECS is the single source of truth for the tag list.

    The UI tag picker renders from it and validation derives from it, so these
    guard the property that made the old hardcoded hint drift out of date.
    """

    def test_validation_sets_derive_from_the_table(self):
        assert _KNOWN_TAGS == {s.name for s in TAG_SPECS}
        assert _YN_TAGS == {s.name for s in TAG_SPECS if s.yes_no}

    def test_no_duplicate_or_empty_entries(self):
        names = [s.name for s in TAG_SPECS]
        assert len(names) == len(set(names))
        assert all(s.name and s.value_hint and s.description for s in TAG_SPECS)

    def test_labels_are_unique(self):
        # The picker resolves a selection back to its spec by label.
        labels = [s.label for s in TAG_SPECS]
        assert len(labels) == len(set(labels))

    @pytest.mark.parametrize("spec", TAG_SPECS, ids=lambda s: s.name)
    def test_documented_example_is_a_valid_query(self, spec):
        """Every value_hint shown to the user must actually parse and validate."""
        tags, _ = _parse_tags(f"{{{spec.name}}}:{spec.value_hint}")
        assert tags, f"{spec.name} hint did not parse as a tag"
        assert not _has_invalid_tags(tags)

    @pytest.mark.parametrize("spec", TAG_SPECS, ids=lambda s: s.name)
    def test_inserted_text_is_valid_or_inert(self, spec):
        """What the picker inserts must never read as an *invalid* tag.

        Yes/no tags arrive complete ("{favorite}:y"). The rest stop at the
        colon, which deliberately doesn't match the tag regex yet — so the
        search icon can't flash red while the user is still typing the value.
        """
        tags, _ = _parse_tags(spec.insert_text)
        if spec.yes_no:
            assert tags and not _has_invalid_tags(tags)
        else:
            assert not tags


# ── filter_songs against a real mini-library ─────────────────────────────────

@pytest.fixture()
def library(tmp_path):
    """Three real SongInfo objects + player stats + favorites.

    A: Freedom Dive / xi / Psi, 222bpm, E+(label Vibro), requires Chroma,
       custom tag "speed", played with FC.
    B: Ghost Rule / DECO*27 / Fefy, 145bpm, Easy+Normal, suggests Cinema,
       played without FC, favorited.
    C: Brain Power / NOMA / Psi, 170bpm, Expert, requires Noodle+Mapping
       Extensions, cinema manifest on disk, unplayed.
    """
    from conftest import make_song_folder, v2_info
    from libraries.song_data import save_custom_tags

    fa = make_song_folder(tmp_path, "a1 (Freedom Dive - Psi)", v2_info(
        title="Freedom Dive", artist="xi", mapper="Psi", bpm=222.0,
        diffs=[("ExpertPlus", {"_difficultyLabel": "Vibro",
                               "_requirements": ["Chroma"]})],
    ))
    save_custom_tags(fa, {"speed"})
    fb = make_song_folder(tmp_path, "b2 (Ghost Rule - Fefy)", v2_info(
        title="Ghost Rule", artist="DECO*27", mapper="Fefy", bpm=145.0,
        diffs=[("Easy", None), ("Normal", {"_suggestions": ["Cinema"]})],
    ))
    fc_ = make_song_folder(tmp_path, "c3 (Brain Power - Psi)", v2_info(
        title="Brain Power", artist="NOMA", mapper="Psi", bpm=170.0,
        diffs=[("Expert", {"_requirements": ["Noodle Extensions",
                                             "Mapping Extensions"]})],
    ))
    (fc_ / "cinema-video.json").write_text(json.dumps({"videoID": "x"}))

    a, b, c = SongInfo(fa), SongInfo(fb), SongInfo(fc_)
    a.song_hash, b.song_hash, c.song_hash = "AAA111", "BBB222", "CCC333"

    stats = {
        "custom_level_AAA111": {4: DiffStat(900_000, "SS", 12, full_combo=True)},
        "custom_level_BBB222": {0: DiffStat(300_000, "B", 3, full_combo=False)},
    }
    favorites = {"custom_level_BBB222"}
    return dict(songs=[a, b, c], a=a, b=b, c=c, stats=stats, favs=favorites)


def _run(lib, query):
    return filter_songs(lib["songs"], query, lib["stats"], lib["favs"])


class TestFilterSongs:
    def test_empty_query_returns_all(self, library):
        assert _run(library, "") == library["songs"]

    def test_plain_matches_title_artist_mapper_and_key(self, library):
        assert _run(library, "ghost") == [library["b"]]
        assert _run(library, "deco") == [library["b"]]
        assert _run(library, "a1") == [library["a"]]

    def test_mapper_substring(self, library):
        assert _run(library, "{mapper}:psi") == [library["a"], library["c"]]

    def test_artist_comma_list_is_or(self, library):
        assert _run(library, "{artist}:xi,noma") == [library["a"], library["c"]]

    def test_title_tag(self, library):
        assert _run(library, "{title}:brain") == [library["c"]]

    def test_unplayed(self, library):
        assert _run(library, "{unplayed}:y") == [library["c"]]
        assert _run(library, "{unplayed}:n") == [library["a"], library["b"]]

    def test_favorite(self, library):
        assert _run(library, "{favorite}:y") == [library["b"]]
        assert _run(library, "{favorite}:n") == [library["a"], library["c"]]

    def test_fullcombo_and_alias(self, library):
        assert _run(library, "{fullcombo}:y") == [library["a"]]
        assert _run(library, "{fc}:n") == [library["b"], library["c"]]

    def test_bpm_ops_and_range(self, library):
        assert _run(library, "{bpm}:>200") == [library["a"]]
        assert _run(library, "{bpm}:=145") == [library["b"]]
        assert _run(library, "{bpm}:>=150 {bpm}:<=200") == [library["c"]]

    def test_difficulty_by_name_number_and_custom_label(self, library):
        assert _run(library, "{difficulty}:expertplus") == [library["a"]]
        assert _run(library, "{difficulty}:4") == [library["a"]]
        assert _run(library, "{difficulty}:easy") == [library["b"]]
        assert _run(library, "{difficulty}:vibro") == [library["a"]]

    def test_custom_tag_exact_match(self, library):
        assert _run(library, "{custom}:speed") == [library["a"]]
        assert _run(library, "{custom}:spee") == []

    def test_mod_tags(self, library):
        assert _run(library, "{chroma}:y") == [library["a"]]
        assert _run(library, "{noodle}:y") == [library["c"]]
        assert _run(library, "{extensions}:y") == [library["c"]]
        assert _run(library, "{noodle}:n {chroma}:n {extensions}:n") == [library["b"]]

    def test_cinema_tag_matches_manifest_and_suggestion(self, library):
        # B suggests Cinema, C ships a manifest — both count.
        assert _run(library, "{cinema}:y") == [library["b"], library["c"]]
        assert _run(library, "{cinema}:n") == [library["a"]]

    def test_tags_combine_as_and(self, library):
        assert _run(library, "{mapper}:psi {unplayed}:y") == [library["c"]]

    def test_plain_and_tags_combine(self, library):
        assert _run(library, "{mapper}:psi freedom") == [library["a"]]

    def test_parsed_passthrough_matches_inline_parse(self, library):
        q = "{mapper}:psi {bpm}:>200"
        direct = filter_songs(library["songs"], q, library["stats"], library["favs"])
        pre = filter_songs(
            library["songs"], q, library["stats"], library["favs"],
            parsed=_parse_tags(q),
        )
        assert direct == pre == [library["a"]]
