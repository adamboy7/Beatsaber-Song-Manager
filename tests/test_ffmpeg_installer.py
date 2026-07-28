"""ffmpeg_installer — archive extraction and the swap over live binaries.

The regression under test: reinstalling to recover a missing ffprobe used to
write straight over ffmpeg/ffplay/ffprobe, so on Windows a playing song (ffplay
live, plus ffmpeg when the visualizer is open) made the install fail partway —
after clobbering some binaries and before writing the one being repaired.

Windows file locking can't be reproduced on Linux CI, so ``in_use`` is stubbed
to mark chosen names as locked; the tests then assert on the swap's behaviour.
"""

from __future__ import annotations

import io
import os
import tarfile
import zipfile
from pathlib import Path

import pytest

from libraries import ffmpeg_installer as fi
from libraries import platform_utils

EXES = tuple(platform_utils.exe_name(n) for n in ("ffmpeg", "ffprobe", "ffplay"))
FFMPEG, FFPROBE, FFPLAY = EXES


def make_zip(path: Path, contents: dict[str, bytes], top="ffmpeg-master-latest-win64-gpl"):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{top}/README.txt", b"ignore me")
        for name, data in contents.items():
            zf.writestr(f"{top}/bin/{name}", data)
    return path


def make_tar(path: Path, contents: dict[str, bytes], top="ffmpeg-master-latest-linux64-gpl"):
    with tarfile.open(path, "w:xz") as tf:
        for name, data in contents.items():
            info = tarfile.TarInfo(f"{top}/bin/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def full_archive(tmp_path, **kw):
    return make_zip(
        tmp_path / "ffmpeg.zip",
        {FFMPEG: b"new-ffmpeg", FFPROBE: b"new-ffprobe", FFPLAY: b"new-ffplay"},
        **kw,
    )


@pytest.fixture()
def dest(tmp_path):
    d = tmp_path / "appdata"
    d.mkdir()
    return d


@pytest.fixture()
def lock(monkeypatch):
    """Pretend the named files are running executables held open by Windows."""
    def _lock(*names):
        locked = set(names)
        monkeypatch.setattr(
            fi, "in_use", lambda p: Path(p).name in locked and Path(p).exists()
        )
    return _lock


def read(path: Path) -> bytes:
    return path.read_bytes()


class TestExtractionBasics:
    def test_extracts_the_three_executables_flat(self, tmp_path, dest):
        archive = full_archive(tmp_path)

        res = fi._extract_exes(archive, dest)

        assert sorted(res.installed) == sorted(EXES)
        assert res.skipped == []
        assert read(dest / FFPROBE) == b"new-ffprobe"

    def test_ignores_non_bin_entries(self, tmp_path, dest):
        archive = full_archive(tmp_path)

        fi._extract_exes(archive, dest)

        assert not (dest / "README.txt").exists()

    def test_handles_tar_xz_archives(self, tmp_path, dest):
        archive = make_tar(tmp_path / "ffmpeg.tar.xz", {FFMPEG: b"tar-ffmpeg"})

        res = fi._extract_exes(archive, dest)

        assert res.installed == [FFMPEG]
        assert read(dest / FFMPEG) == b"tar-ffmpeg"

    def test_found_covers_installed_and_skipped(self, tmp_path, dest, lock):
        """``found`` is what the archive contained, however each one was handled
        — the caller's "did we get an ffmpeg?" check depends on it."""
        (dest / FFPLAY).write_bytes(b"old-ffplay")
        lock(FFPLAY)

        res = fi._extract_exes(full_archive(tmp_path), dest)

        assert sorted(res.found) == sorted(EXES)

    def test_bad_archive_raises_and_leaves_nothing_behind(self, tmp_path, dest):
        bad = tmp_path / "broken.zip"
        bad.write_bytes(b"not a zip")

        with pytest.raises(fi.FfmpegInstallError, match="could not extract"):
            fi._extract_exes(bad, dest)

        assert list(dest.iterdir()) == []

    def test_no_part_files_survive_a_success(self, tmp_path, dest):
        fi._extract_exes(full_archive(tmp_path), dest)

        assert [p.name for p in dest.iterdir() if p.name.endswith(".part")] == []


class TestRunningBinariesAreSkipped:
    def test_repairs_ffprobe_while_ffplay_is_running(self, tmp_path, dest, lock):
        """The reported bug: a song is playing, so ffplay is live (it's the
        playback backend without libmpv) and ffprobe is what we're restoring."""
        (dest / FFMPEG).write_bytes(b"old-ffmpeg")
        (dest / FFPLAY).write_bytes(b"old-ffplay")
        lock(FFPLAY)

        res = fi._extract_exes(full_archive(tmp_path), dest)

        assert read(dest / FFPROBE) == b"new-ffprobe"  # the actual repair
        assert read(dest / FFMPEG) == b"new-ffmpeg"    # not in use, replaced
        assert read(dest / FFPLAY) == b"old-ffplay"    # in use, left alone
        assert res.skipped == [FFPLAY]
        assert res.installed == [FFMPEG, FFPROBE]

    def test_a_successful_install_leaves_no_residue(self, tmp_path, dest, lock):
        """The whole point: no .old file for the running binary, because it was
        never moved in the first place."""
        (dest / FFPLAY).write_bytes(b"old-ffplay")
        lock(FFPLAY)

        fi._extract_exes(full_archive(tmp_path), dest)

        leftovers = [p.name for p in dest.iterdir() if fi._leftover_base(p.name)]
        assert leftovers == []
        assert sorted(p.name for p in dest.iterdir()) == sorted(EXES)

    def test_all_three_locked_changes_nothing(self, tmp_path, dest, lock):
        for name in EXES:
            (dest / name).write_bytes(b"old")
        lock(*EXES)

        res = fi._extract_exes(full_archive(tmp_path), dest)

        assert sorted(res.skipped) == sorted(EXES)
        assert res.installed == []
        for name in EXES:
            assert read(dest / name) == b"old"
        assert [p.name for p in dest.iterdir() if fi._leftover_base(p.name)] == []

    def test_nothing_locked_replaces_everything(self, tmp_path, dest, lock):
        for name in EXES:
            (dest / name).write_bytes(b"old")
        lock()

        res = fi._extract_exes(full_archive(tmp_path), dest)

        assert res.skipped == []
        assert read(dest / FFMPEG) == b"new-ffmpeg"
        assert not [p for p in dest.iterdir() if fi._SUPERSEDED_PREFIX in p.name]

    def test_absent_binary_is_not_treated_as_in_use(self, tmp_path, dest, lock):
        """A first-time install has no existing files to be locked."""
        lock(*EXES)  # in_use() still returns False for a path that doesn't exist

        res = fi._extract_exes(full_archive(tmp_path), dest)

        assert res.skipped == []
        assert sorted(res.installed) == sorted(EXES)


class TestSwapFailureIsAtomic:
    def test_rollback_restores_every_original(self, tmp_path, dest, monkeypatch):
        """No partially-updated set: the old failure mode clobbered ffmpeg and
        then died before writing ffprobe."""
        originals = {n: f"old-{n}".encode() for n in EXES}
        for name, data in originals.items():
            (dest / name).write_bytes(data)

        real_replace = os.replace
        placements: list[str] = []

        def flaky(src, dst):
            # Only interfere with phase 2 (staged .part files moving into
            # place), and only on the last one — the rollback's own moves must
            # be allowed through.
            if str(src).endswith(fi._PART_SUFFIX):
                placements.append(Path(dst).name)
                if len(placements) >= len(EXES):
                    raise OSError("disk full")
            return real_replace(src, dst)

        monkeypatch.setattr(fi.os, "replace", flaky)

        with pytest.raises(fi.FfmpegInstallError, match="could not install"):
            fi._extract_exes(full_archive(tmp_path), dest)

        for name, data in originals.items():
            assert read(dest / name) == data, f"{name} was not rolled back"

    def test_rollback_spares_the_skipped_binary(self, tmp_path, dest, lock, monkeypatch):
        """With one binary running and the swap of another failing, the running
        one is untouched and the rest roll back — no mixed set either way."""
        for name in EXES:
            (dest / name).write_bytes(f"old-{name}".encode())
        lock(FFPLAY)

        real_replace = os.replace

        def flaky(src, dst):
            if str(src).endswith(fi._PART_SUFFIX):
                raise OSError("nope")
            return real_replace(src, dst)

        monkeypatch.setattr(fi.os, "replace", flaky)

        with pytest.raises(fi.FfmpegInstallError, match="could not install"):
            fi._extract_exes(full_archive(tmp_path), dest)

        for name in EXES:
            assert read(dest / name) == f"old-{name}".encode()

    def test_failed_install_leaves_no_scratch_files(self, tmp_path, dest, monkeypatch):
        for name in EXES:
            (dest / name).write_bytes(b"old")

        real_replace = os.replace

        def flaky(src, dst):
            if str(src).endswith(fi._PART_SUFFIX):
                raise OSError("nope")
            return real_replace(src, dst)

        monkeypatch.setattr(fi.os, "replace", flaky)

        with pytest.raises(fi.FfmpegInstallError):
            fi._extract_exes(full_archive(tmp_path), dest)

        fi.reap_superseded(dest)
        assert sorted(p.name for p in dest.iterdir()) == sorted(EXES)


class TestSupersededCleanup:
    def test_leftovers_are_reaped_on_the_next_install(self, tmp_path, dest, lock):
        stale = dest / f"{FFPLAY}{fi._SUPERSEDED_PREFIX}9999"
        stale.write_bytes(b"ancient")
        stale_part = dest / f"{FFMPEG}{fi._PART_SUFFIX}"
        stale_part.write_bytes(b"half-written")

        fi._extract_exes(full_archive(tmp_path), dest)

        assert not stale.exists()
        assert not stale_part.exists()

    def test_cleanup_ignores_real_binaries(self, tmp_path, dest):
        (dest / FFMPEG).write_bytes(b"keep me")

        fi.reap_superseded(dest)

        assert read(dest / FFMPEG) == b"keep me"

    def test_cleanup_leaves_unrelated_app_data_alone(self, tmp_path, dest):
        """dest_dir is the shared app-data folder — config, hash cache and
        libmpv live here too."""
        neighbours = {
            "config.json": b"{}",
            ".bsm_hash_cache.json": b"{}",
            "libmpv-2.dll": b"dll",
            "unrelated.part": b"someone else's scratch file",
            "notes.old-1": b"not ours either",
        }
        for name, data in neighbours.items():
            (dest / name).write_bytes(data)

        fi.reap_superseded(dest)

        for name, data in neighbours.items():
            assert (dest / name).exists(), f"{name} was deleted"
            assert read(dest / name) == data

    def test_cleanup_tolerates_a_missing_directory(self, tmp_path):
        fi.reap_superseded(tmp_path / "nope")  # must not raise

    def test_locked_leftover_is_left_for_a_later_run(self, tmp_path, dest, monkeypatch):
        stale = dest / f"{FFPLAY}{fi._SUPERSEDED_PREFIX}1"
        stale.write_bytes(b"still running")

        def refuse(self, missing_ok=False):
            raise OSError("locked")

        monkeypatch.setattr(Path, "unlink", refuse)
        fi.reap_superseded(dest)  # must not raise

        assert stale.exists()

    def test_old_files_from_a_previous_version_are_cleaned_up(self, tmp_path, dest):
        """An earlier build renamed running binaries aside instead of skipping
        them, so upgrading users may still have these."""
        for name in (f"{FFPLAY}{fi._SUPERSEDED_PREFIX}1",
                     f"{FFMPEG}{fi._SUPERSEDED_PREFIX}2"):
            (dest / name).write_bytes(b"old")

        fi.reap_superseded(dest)

        assert list(dest.iterdir()) == []


class TestInUseProbe:
    def test_absent_file_is_not_in_use(self, dest):
        assert fi.in_use(dest / "absent.exe") is False

    def test_writable_file_is_not_in_use(self, dest):
        p = dest / FFMPEG
        p.write_bytes(b"x")
        assert fi.in_use(p) is False

    def test_unwritable_file_reads_as_in_use(self, dest, monkeypatch):
        p = dest / FFMPEG
        p.write_bytes(b"x")

        def refuse(*a, **kw):
            raise PermissionError("locked by another process")

        monkeypatch.setattr("builtins.open", refuse)
        assert fi.in_use(p) is True

    def test_binaries_in_use_lists_running_ones(self, dest, lock):
        for name in EXES:
            (dest / name).write_bytes(b"x")
        lock(FFPLAY, FFMPEG)

        assert sorted(fi.binaries_in_use(dest)) == sorted([FFMPEG, FFPLAY])
