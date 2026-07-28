"""playlist_installer — which entries get fetched, and how the run is counted.

The installer's whole job is deciding what to ask BeatSaver for, so
``beatsaver_api`` is stubbed out and the tests assert on the recorded calls.
Installs run on a background thread; ``run_install`` drives them to completion
with a synchronous dispatcher and an event.
"""

from __future__ import annotations

import json
import threading

import pytest

from libraries import playlist_installer as pi
from libraries.beatsaver_api import InstallResult


class FakeBeatSaver:
    """Records install calls. ``present`` hashes/keys report already-installed,
    ``broken`` ones raise."""

    def __init__(self, present=(), broken=()):
        self.by_hash: list[str] = []
        self.by_key: list[str] = []
        self.present = {str(p).upper() for p in present}
        self.broken = {str(b).upper() for b in broken}

    def _result(self, ident, custom_levels):
        if ident.upper() in self.broken:
            raise RuntimeError("boom")
        return InstallResult(
            custom_levels / ident, ident.upper() not in self.present
        )

    def install_by_hash(self, song_hash, custom_levels):
        self.by_hash.append(song_hash)
        return self._result(song_hash, custom_levels)

    def install_song(self, key, custom_levels):
        self.by_key.append(key)
        return self._result(key, custom_levels)


@pytest.fixture()
def fake_bs(monkeypatch):
    def _install(present=(), broken=()):
        fake = FakeBeatSaver(present, broken)
        monkeypatch.setattr(pi.bs, "install_by_hash", fake.install_by_hash)
        monkeypatch.setattr(pi.bs, "install_song", fake.install_song)
        return fake

    return _install


class Harness:
    """Collects status strings and the final success flag."""

    def __init__(self, custom_levels):
        self.status: list[str] = []
        self.success: bool | None = None
        self.done = threading.Event()
        self.installer = pi.PlaylistInstaller(
            custom_levels,
            lambda cb: cb(),  # dispatch inline; worker thread is fine here
            self.status.append,
            self._complete,
        )

    def _complete(self, success):
        self.success = success
        self.done.set()

    def run(self, *args, **kwargs) -> bool:
        launched = self.installer.install(*args, **kwargs)
        if launched:
            assert self.done.wait(timeout=5), "install did not complete"
        return launched


@pytest.fixture()
def harness(tmp_path):
    return Harness(tmp_path / "CustomLevels")


def write_playlist(tmp_path, songs, name="list.bplist"):
    p = tmp_path / name
    p.write_text(json.dumps({"songs": songs}), encoding="utf-8")
    return p


SONGS = [
    {"hash": "AAA", "songName": "Have it"},
    {"hash": "BBB", "songName": "Missing one"},
    {"hash": "CCC", "songName": "Missing two"},
]


class TestEntryFiltering:
    def test_entries_arg_limits_what_is_fetched(self, tmp_path, harness, fake_bs):
        """The regression: passing pre-filtered entries must not walk the
        whole playlist file."""
        fake = fake_bs()
        path = write_playlist(tmp_path, SONGS)

        harness.run(path, entries=SONGS[1:])

        assert fake.by_hash == ["BBB", "CCC"]

    def test_entries_arg_skips_the_file_entirely(self, tmp_path, harness, fake_bs):
        fake = fake_bs()

        # Path doesn't exist; with explicit entries it is never read.
        assert harness.run(tmp_path / "absent.bplist", entries=SONGS[1:])
        assert fake.by_hash == ["BBB", "CCC"]

    def test_without_entries_walks_the_whole_playlist(self, tmp_path, harness, fake_bs):
        fake = fake_bs()
        path = write_playlist(tmp_path, SONGS)

        harness.run(path)

        assert fake.by_hash == ["AAA", "BBB", "CCC"]

    def test_installed_hashes_filters_before_any_request(self, tmp_path, harness, fake_bs):
        fake = fake_bs()
        path = write_playlist(tmp_path, SONGS)

        harness.run(path, installed_hashes={"AAA"})

        assert fake.by_hash == ["BBB", "CCC"]

    def test_installed_hashes_is_case_insensitive(self, tmp_path, harness, fake_bs):
        fake = fake_bs()
        path = write_playlist(tmp_path, SONGS)

        harness.run(path, installed_hashes={"aaa", "bbb"})

        assert fake.by_hash == ["CCC"]

    def test_hash_preferred_over_key(self, tmp_path, harness, fake_bs):
        fake = fake_bs()
        entries = [{"hash": "AAA", "key": "1abc"}]

        harness.run(tmp_path / "x.bplist", entries=entries)

        assert fake.by_hash == ["AAA"]
        assert fake.by_key == []

    def test_key_used_when_hash_absent(self, tmp_path, harness, fake_bs):
        fake = fake_bs()
        entries = [{"key": "1abc"}, {"id": "2def"}]

        harness.run(tmp_path / "x.bplist", entries=entries)

        assert fake.by_key == ["1abc", "2def"]

    def test_hash_only_entries_are_installed(self, tmp_path, harness, fake_bs):
        """Entries with a hash but no key are fetchable; the caller's filter
        must not be the key-only ``installable_entries``."""
        from libraries.playlist_model import fetchable_entries

        missing = [{"hash": "BBB", "songName": "hash only"}, {"songName": "orphan"}]
        fake = fake_bs()

        harness.run(tmp_path / "x.bplist", entries=fetchable_entries(missing))

        assert fake.by_hash == ["BBB"]


class TestProgressAndSummary:
    def test_progress_total_is_the_filtered_count(self, tmp_path, harness, fake_bs):
        """Status used to read "1/3" when only 2 songs were being installed."""
        fake_bs()
        path = write_playlist(tmp_path, SONGS)

        harness.run(path, entries=SONGS[1:])

        progress = [s for s in harness.status if s.startswith("Installing playlist")]
        assert progress == [
            "Installing playlist — 1/2: Missing one",
            "Installing playlist — 2/2: Missing two",
        ]

    def test_summary_separates_downloaded_from_already_installed(
        self, tmp_path, harness, fake_bs
    ):
        fake_bs(present=["AAA"])
        path = write_playlist(tmp_path, SONGS)

        harness.run(path)

        assert "2 downloaded" in harness.status[-1]
        assert "1 already installed" in harness.status[-1]

    def test_summary_reports_failures(self, tmp_path, harness, fake_bs):
        fake_bs(broken=["CCC"])
        path = write_playlist(tmp_path, SONGS)

        harness.run(path)

        assert "2 downloaded" in harness.status[-1]
        assert "1 failed" in harness.status[-1]

    def test_summary_reports_unfetchable_entries(self, tmp_path, harness, fake_bs):
        fake_bs()
        entries = [{"hash": "BBB"}, {"songName": "no hash, no key"}]

        harness.run(tmp_path / "x.bplist", entries=entries)

        assert "1 skipped (no BeatSaver key)" in harness.status[-1]

    def test_empty_worklist_says_nothing_to_download(self, tmp_path, harness, fake_bs):
        fake = fake_bs()
        path = write_playlist(tmp_path, SONGS)

        harness.run(path, installed_hashes={"AAA", "BBB", "CCC"})

        assert fake.by_hash == []
        assert harness.status[-1] == "Playlist install finished — nothing to download."


class TestSuccessFlag:
    def test_clean_run_succeeds(self, tmp_path, harness, fake_bs):
        fake_bs()
        harness.run(write_playlist(tmp_path, SONGS))
        assert harness.success is True

    def test_nothing_to_do_succeeds(self, tmp_path, harness, fake_bs):
        fake_bs()
        harness.run(tmp_path / "x.bplist", entries=[])
        assert harness.success is True

    def test_already_installed_is_not_a_failure(self, tmp_path, harness, fake_bs):
        fake_bs(present=["AAA", "BBB", "CCC"])
        harness.run(write_playlist(tmp_path, SONGS))
        assert harness.success is True

    def test_any_download_failure_fails(self, tmp_path, harness, fake_bs):
        """Previously true whenever a single entry got through — a partial
        install reported success."""
        fake_bs(broken=["CCC"])
        harness.run(write_playlist(tmp_path, SONGS))
        assert harness.success is False

    def test_unfetchable_entry_alone_is_not_a_failure(self, tmp_path, harness, fake_bs):
        fake_bs()
        harness.run(tmp_path / "x.bplist", entries=[{"songName": "orphan"}])
        assert harness.success is True


class TestBadInput:
    def test_missing_file_returns_false_without_completing(self, tmp_path, harness, fake_bs):
        fake_bs()
        assert harness.run(tmp_path / "absent.bplist") is False
        assert harness.success is None
        assert "Playlist not found" in harness.status[-1]

    def test_unreadable_file_returns_false_without_completing(self, tmp_path, harness, fake_bs):
        fake_bs()
        path = tmp_path / "bad.bplist"
        path.write_text("{nope", encoding="utf-8")

        assert harness.run(path) is False
        assert harness.success is None
        assert "Could not read playlist" in harness.status[-1]

    def test_playlist_with_no_songs_key(self, tmp_path, harness, fake_bs):
        fake = fake_bs()
        path = tmp_path / "empty.bplist"
        path.write_text(json.dumps({"playlistTitle": "T"}), encoding="utf-8")

        assert harness.run(path) is True
        assert fake.by_hash == []
        assert harness.success is True


class TestCancellation:
    def test_cancel_before_completion_suppresses_callback(self, tmp_path, monkeypatch):
        """A superseded generation must not fire complete_cb."""
        h = Harness(tmp_path / "CustomLevels")
        started = threading.Event()
        release = threading.Event()

        def slow(song_hash, custom_levels):
            started.set()
            release.wait(timeout=5)
            return InstallResult(custom_levels / song_hash, True)

        monkeypatch.setattr(pi.bs, "install_by_hash", slow)

        assert h.installer.install(tmp_path / "x.bplist", entries=SONGS)
        assert started.wait(timeout=5)
        h.installer.cancel()
        release.set()

        assert not h.done.wait(timeout=1)
        assert h.success is None
