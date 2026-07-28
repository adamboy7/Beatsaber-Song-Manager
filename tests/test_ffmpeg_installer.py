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

        assert sorted(res.written) == sorted(EXES)
        assert res.deferred == []
        assert read(dest / FFPROBE) == b"new-ffprobe"

    def test_ignores_non_bin_entries(self, tmp_path, dest):
        archive = full_archive(tmp_path)

        fi._extract_exes(archive, dest)

        assert not (dest / "README.txt").exists()

    def test_handles_tar_xz_archives(self, tmp_path, dest):
        archive = make_tar(tmp_path / "ffmpeg.tar.xz", {FFMPEG: b"tar-ffmpeg"})

        res = fi._extract_exes(archive, dest)

        assert res.written == [FFMPEG]
        assert read(dest / FFMPEG) == b"tar-ffmpeg"

    def test_bad_archive_raises_and_leaves_nothing_behind(self, tmp_path, dest):
        bad = tmp_path / "broken.zip"
        bad.write_bytes(b"not a zip")

        with pytest.raises(fi.FfmpegInstallError, match="could not extract"):
            fi._extract_exes(bad, dest)

        assert list(dest.iterdir()) == []

    def test_no_part_files_survive_a_success(self, tmp_path, dest):
        fi._extract_exes(full_archive(tmp_path), dest)

        assert [p.name for p in dest.iterdir() if p.name.endswith(".part")] == []


class TestSwapOverRunningBinaries:
    def test_repairs_ffprobe_while_ffplay_is_running(self, tmp_path, dest, lock):
        """The reported bug: a song is playing, so ffplay is live and ffprobe is
        the thing we're trying to restore."""
        (dest / FFMPEG).write_bytes(b"old-ffmpeg")
        (dest / FFPLAY).write_bytes(b"old-ffplay")
        lock(FFPLAY)

        res = fi._extract_exes(full_archive(tmp_path), dest)

        assert read(dest / FFPROBE) == b"new-ffprobe"  # the actual repair
        assert read(dest / FFPLAY) == b"new-ffplay"    # swapped in behind the lock
        assert res.deferred == [FFPLAY]

    def test_running_process_keeps_its_copy(self, tmp_path, dest, lock):
        (dest / FFPLAY).write_bytes(b"old-ffplay")
        lock(FFPLAY)

        fi._extract_exes(full_archive(tmp_path), dest)

        superseded = [p for p in dest.iterdir() if fi._SUPERSEDED_PREFIX in p.name]
        assert len(superseded) == 1
        assert read(superseded[0]) == b"old-ffplay"

    def test_all_three_locked_still_installs(self, tmp_path, dest, lock):
        for name in EXES:
            (dest / name).write_bytes(b"old")
        lock(*EXES)

        res = fi._extract_exes(full_archive(tmp_path), dest)

        assert sorted(res.deferred) == sorted(EXES)
        assert read(dest / FFMPEG) == b"new-ffmpeg"
        assert read(dest / FFPROBE) == b"new-ffprobe"

    def test_nothing_locked_reports_no_deferral(self, tmp_path, dest, lock):
        for name in EXES:
            (dest / name).write_bytes(b"old")
        lock()

        res = fi._extract_exes(full_archive(tmp_path), dest)

        assert res.deferred == []
        assert not [p for p in dest.iterdir() if fi._SUPERSEDED_PREFIX in p.name]


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

    def test_rollback_when_target_was_locked(self, tmp_path, dest, lock, monkeypatch):
        for name in EXES:
            (dest / name).write_bytes(f"old-{name}".encode())
        lock(*EXES)

        real_replace = os.replace

        def flaky(src, dst):
            if str(src).endswith(fi._PART_SUFFIX):
                raise OSError("nope")
            return real_replace(src, dst)

        monkeypatch.setattr(fi.os, "replace", flaky)

        with pytest.raises(fi.FfmpegInstallError):
            fi._extract_exes(full_archive(tmp_path), dest)

        for name in EXES:
            assert read(dest / name) == f"old-{name}".encode()


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

        fi._clear_superseded(dest)

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

        fi._clear_superseded(dest)

        for name, data in neighbours.items():
            assert (dest / name).exists(), f"{name} was deleted"
            assert read(dest / name) == data

    def test_cleanup_tolerates_a_missing_directory(self, tmp_path):
        fi._clear_superseded(tmp_path / "nope")  # must not raise

    def test_locked_leftover_is_left_for_a_later_run(self, tmp_path, dest, monkeypatch):
        stale = dest / f"{FFPLAY}{fi._SUPERSEDED_PREFIX}1"
        stale.write_bytes(b"still running")

        def refuse(self, missing_ok=False):
            raise OSError("locked")

        monkeypatch.setattr(Path, "unlink", refuse)
        fi._clear_superseded(dest)  # must not raise

        assert stale.exists()


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
