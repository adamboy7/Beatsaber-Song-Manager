"""Headless CLI (Browser.main) — --randomAdd / --shuffle / --install arg logic.

Runs main() in-process with the library detection and player data
monkeypatched, so no real Beat Saber install, network, or window is ever
touched. Requires the full GUI dependency set (tkinter, tkinterdnd2, PIL) to
*import* Browser.py, so the module skips cleanly on headless boxes without Tk
— CI installs python3-tk to run it.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

import conftest as _conftest

if not _conftest.HAS_REAL_TK:
    pytest.skip("real tkinter required to import Browser.py", allow_module_level=True)
pytest.importorskip("tkinterdnd2")
pytest.importorskip("PIL")

import Browser  # noqa: E402
from conftest import make_song_folder, v2_info  # noqa: E402


def _real_cover(folder):
    """Replace the factory's fake cover bytes with a decodable PNG so the
    new-playlist art-embedding path can run."""
    from PIL import Image
    Image.new("RGB", (4, 4), "magenta").save(folder / "cover.jpg", format="PNG")


@pytest.fixture()
def cli(tmp_path, monkeypatch, capsys):
    """A 6-song library (2 mapped by Psi) wired into Browser.main()."""
    from libraries import app_config
    from libraries.song_data import compute_song_hash

    levels = tmp_path / "CustomLevels"
    levels.mkdir()
    specs = [
        ("a1 (Alpha - Psi)", dict(title="Alpha", mapper="Psi")),
        ("b2 (Beta - Psi)", dict(title="Beta", mapper="Psi")),
        ("c3 (Gamma - Fefy)", dict(title="Gamma", mapper="Fefy")),
        ("d4 (Delta - Fefy)", dict(title="Delta", mapper="Fefy")),
        ("e5 (Epsilon - Uadyet)", dict(title="Epsilon", mapper="Uadyet")),
        ("f6 (Zeta - Uadyet)", dict(title="Zeta", mapper="Uadyet")),
    ]
    hashes: dict[str, str] = {}
    psi_hashes: set[str] = set()
    for name, kw in specs:
        folder = make_song_folder(levels, name, v2_info(**kw))
        _real_cover(folder)
        h = compute_song_hash(folder)
        hashes[name] = h
        if kw["mapper"] == "Psi":
            psi_hashes.add(h)

    appdata = tmp_path / "appdata"
    appdata.mkdir()
    monkeypatch.setattr(app_config, "_app_data_dir", appdata)
    monkeypatch.setattr(Browser, "find_beatsaber_custom_levels", lambda: levels)
    monkeypatch.setattr(Browser, "find_player_data", lambda: (None, ""))

    # Any test that reaches the GUI branch is a bug — fail instead of
    # opening a real window (or hanging in mainloop).
    def _no_gui(*a, **kw):
        raise AssertionError("GUI path reached in a headless CLI test")
    monkeypatch.setattr(Browser, "SongBrowser", _no_gui)
    monkeypatch.setattr(Browser, "_resolve_startup_custom_levels", _no_gui)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["Browser.py", *map(str, argv)])
        code = None
        try:
            Browser.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 0
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return SimpleNamespace(
        run=run, tmp=tmp_path,
        all_hashes=set(hashes.values()), hashes=hashes, psi=psi_hashes,
    )


def _songs_in(path):
    return json.loads(path.read_text(encoding="utf-8"))["songs"]


class TestRandomAdd:
    def test_creates_new_playlist_with_unique_picks_and_art(self, cli):
        out_file = cli.tmp / "new.bplist"
        # Playlist arg deliberately *after* the greedy --randomAdd, to cover
        # the argv reordering pre-pass.
        code, out, _ = cli.run("--randomAdd", "3", out_file)
        assert code == 0
        assert "Created" in out
        songs = _songs_in(out_file)
        assert len(songs) == 3
        picked = {s["hash"] for s in songs}
        assert len(picked) == 3 and picked <= cli.all_hashes
        assert all(s["songName"] and s["key"] for s in songs)
        data = json.loads(out_file.read_text(encoding="utf-8"))
        assert data["image"]  # cover art embedded from the first pick

    def test_filter_restricts_picks(self, cli):
        out_file = cli.tmp / "psi.bplist"
        code, _, _ = cli.run("--randomAdd", "2", "{mapper}:psi", out_file)
        assert code == 0
        assert {s["hash"] for s in _songs_in(out_file)} == cli.psi

    def test_append_skips_hashes_already_in_playlist(self, cli):
        out_file = cli.tmp / "grow.bplist"
        existing = sorted(cli.all_hashes)[:4]
        out_file.write_text(json.dumps({
            "playlistTitle": "T", "songs": [{"hash": h} for h in existing],
        }))
        code, out, _ = cli.run(out_file, "--randomAdd", "2")
        assert code == 0
        assert "appended 2" in out.lower()
        final = [s["hash"] for s in _songs_in(out_file)]
        assert len(final) == 6
        assert set(final) == cli.all_hashes  # exactly the 2 missing ones added

    def test_fully_covered_playlist_warns_and_adds_nothing(self, cli):
        out_file = cli.tmp / "full.bplist"
        out_file.write_text(json.dumps({
            "songs": [{"hash": h} for h in cli.all_hashes],
        }))
        code, out, _ = cli.run(out_file, "--randomAdd", "3")
        assert code == 0
        assert "no candidate songs" in out.lower()
        assert len(_songs_in(out_file)) == 6

    def test_multiple_groups_do_not_overlap(self, cli):
        out_file = cli.tmp / "multi.bplist"
        code, _, _ = cli.run(
            "--randomAdd", "2", "{mapper}:psi", "--randomAdd", "2", out_file
        )
        assert code == 0
        picked = [s["hash"] for s in _songs_in(out_file)]
        assert len(picked) == 4
        assert len(set(picked)) == 4

    def test_lowercase_flag_spelling_accepted(self, cli):
        out_file = cli.tmp / "lower.bplist"
        code, _, _ = cli.run("--randomadd", "2", out_file)
        assert code == 0
        assert len(_songs_in(out_file)) == 2

    @pytest.mark.parametrize("count", ["abc", "0", "-3"])
    def test_invalid_count_exits_1(self, cli, count):
        code, _, err = cli.run("--randomAdd", count, cli.tmp / "x.bplist")
        assert code == 1
        assert "--randomAdd" in err


class TestShuffle:
    def test_shuffles_existing_playlist_in_place(self, cli):
        out_file = cli.tmp / "shuf.bplist"
        original = [{"hash": f"H{i}", "songName": f"S{i}"} for i in range(8)]
        out_file.write_text(json.dumps({"playlistTitle": "T", "songs": original}))
        code, out, _ = cli.run(out_file, "--shuffle")
        assert code == 0
        assert "shuffled 8" in out.lower()
        after = _songs_in(out_file)
        assert {s["hash"] for s in after} == {s["hash"] for s in original}
        assert len(after) == 8

    def test_shuffle_without_playlist_or_randomadd_exits_1(self, cli):
        code, out, _ = cli.run(cli.tmp / "absent.bplist", "--shuffle")
        assert code == 1
        assert "does not exist" in out

    def test_randomadd_then_shuffle_creates_file(self, cli):
        out_file = cli.tmp / "combo.bplist"
        code, out, _ = cli.run(out_file, "--randomAdd", "4", "--shuffle")
        assert code == 0
        assert "shuffled" in out.lower()
        assert len(_songs_in(out_file)) == 4


class TestInstallArgValidation:
    def test_install_without_playlist_exits_1(self, cli):
        code, out, _ = cli.run("--install")
        assert code == 1
        assert "requires" in out

    def test_install_precedence_note_on_combined_flags(self, cli):
        code, _, err = cli.run("--install", "--shuffle")
        assert code == 1  # still fails validation, but the note fires first
        assert "precedence" in err


class TestBadPlaylistFile:
    def test_invalid_json_exits_1(self, cli):
        bad = cli.tmp / "bad.bplist"
        bad.write_text("{nope", encoding="utf-8")
        code, out, _ = cli.run(bad, "--shuffle")
        assert code == 1
        assert "not valid JSON" in out

    def test_missing_songs_array_exits_1(self, cli):
        bad = cli.tmp / "nosongs.bplist"
        bad.write_text(json.dumps({"playlistTitle": "T"}), encoding="utf-8")
        code, out, _ = cli.run(bad, "--shuffle")
        assert code == 1
        assert "no 'songs' array" in out
