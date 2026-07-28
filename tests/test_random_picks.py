"""pick_random_songs — the CLI --randomAdd / queue-Replace pick-priority ladder:
filtered first, unfiltered supplement, repeats only as a last resort."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from libraries.browser_pagination import pick_random_songs


def _songs(prefix, n):
    return [SimpleNamespace(folder=f"{prefix}{i}") for i in range(n)]


LIB = _songs("lib", 10)


class TestPickRandomSongs:
    def test_fill_from_filtered_only(self):
        filtered = LIB[:5]
        picks = pick_random_songs(filtered, LIB, 3)
        assert len(picks) == 3
        assert all(p in filtered for p in picks)
        assert len({p.folder for p in picks}) == 3  # no repeats

    def test_supplement_from_unfiltered_without_duplicates(self):
        filtered = LIB[:2]
        picks = pick_random_songs(filtered, LIB, 6)
        assert len(picks) == 6
        assert {p.folder for p in picks[:2]} == {s.folder for s in filtered}  # filtered first
        assert len({p.folder for p in picks}) == 6

    def test_repeats_only_when_library_exhausted(self, capsys):
        picks = pick_random_songs(LIB[:2], LIB, 15)
        assert len(picks) == 15
        # Every library song appears before any repeat is used.
        assert {p.folder for p in picks} == {s.folder for s in LIB}
        assert "duplicates" in capsys.readouterr().out

    def test_exact_library_size_has_no_repeats(self):
        picks = pick_random_songs(None, LIB, 10)
        assert sorted(p.folder for p in picks) == sorted(s.folder for s in LIB)

    def test_empty_or_none_filtered_uses_unfiltered(self):
        for filtered in (None, []):
            picks = pick_random_songs(filtered, LIB, 4)
            assert len(picks) == 4
            assert len({p.folder for p in picks}) == 4

    def test_zero_picks(self):
        assert pick_random_songs(LIB[:3], LIB, 0) == []

    def test_n_larger_than_everything_pads_with_repeats(self):
        tiny = _songs("t", 2)
        picks = pick_random_songs(None, tiny, 5)
        assert len(picks) == 5
        assert {p.folder for p in picks} == {"t0", "t1"}

    def test_randomness_actually_varies(self):
        """Not a fixed prefix of the pool — sanity check it samples."""
        import random
        seen = set()
        for seed in range(20):
            random.seed(seed)
            seen.add(tuple(p.folder for p in pick_random_songs(None, LIB, 3)))
        assert len(seen) > 1
