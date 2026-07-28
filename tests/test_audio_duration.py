"""audio_utils.get_audio_duration — the probe chain and its failure reporting.

The regression under test: playing a song with no ffprobe and no libmpv popped
the "reinstall ffmpeg" prompt even though the built-in reader should have
answered. Two separate causes fed it, and neither was an ffmpeg problem:

  * ``mutagen.File(options=None)`` imports 26 format modules from inside the
    function, so one un-bundled module made every read raise ImportError;
  * the bare ``except Exception`` swallowed that ImportError exactly like an
    unparseable file, so a missing reader was reported as broken tooling.
"""

from __future__ import annotations

import builtins
import struct
import sys
import types
from pathlib import Path

import pytest

from libraries import audio_utils as au

pytest.importorskip("mutagen", reason="the built-in reader is the subject here")


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_module_state(monkeypatch):
    """audio_utils caches lookups and 'already notified' in module globals."""
    monkeypatch.setattr(au, "_reader_options", None)
    monkeypatch.setattr(au, "_reader_unavailable", None)
    monkeypatch.setattr(au, "_probe_tools_notified", False)
    monkeypatch.setattr(au, "_probe_fallback_notifier", None)
    monkeypatch.setattr(au, "_ffprobe_cache", None)
    monkeypatch.setattr(au, "_ffmpeg_cache", None)
    yield


@pytest.fixture()
def no_external_tools(monkeypatch):
    """The reported environment: no ffprobe, no libmpv."""
    monkeypatch.setattr(au, "find_ffprobe", lambda: None)
    monkeypatch.setattr(au, "_ffprobe_duration", lambda p: None)
    fake = types.ModuleType("libraries.mpv_backend")
    fake.load_mpv = lambda: None
    monkeypatch.setitem(sys.modules, "libraries.mpv_backend", fake)


@pytest.fixture()
def prompts(monkeypatch):
    """Capture what the UI would be asked to do."""
    seen: list[au.ProbeFailure] = []
    au.set_probe_fallback_notifier(seen.append)
    monkeypatch.setattr(au, "_probe_fallback_notifier", seen.append)
    return seen


@pytest.fixture()
def no_mutagen(monkeypatch):
    """Simulate mutagen absent, or present but missing a format module."""
    def _break(*broken: str):
        targets = set(broken) or {"mutagen"}
        real_import = builtins.__import__

        def guard(name, *a, **kw):
            if name in targets or any(name.startswith(f"{t}.") for t in targets):
                raise ImportError(f"No module named {name!r}")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", guard)

    return _break


def ogg_vorbis(path: Path, seconds: float = 3.0) -> Path:
    """Write a minimal-but-real Ogg Vorbis file mutagen can measure.

    Only the identification header and a final page carrying the end granule
    position are needed: mutagen derives length from granulepos / sample rate.
    """
    rate = 44100

    def page(payload: bytes, granule: int, seq: int, flags: int) -> bytes:
        segments = []
        remaining = len(payload)
        while remaining >= 255:
            segments.append(255)
            remaining -= 255
        segments.append(remaining)
        header = struct.pack(
            "<4sBBqIIiB", b"OggS", 0, flags, granule, 1, seq, 0, len(segments)
        ) + bytes(segments)
        return header + payload

    ident = (b"\x01vorbis" + struct.pack("<IBIiiiB", 0, 2, rate, 0, 0, 0, 0xB8))
    comment = b"\x03vorbis" + struct.pack("<I", 6) + b"pytest" + struct.pack("<I", 0) + b"\x01"
    setup = b"\x05vorbis" + b"\x00" * 32
    data = (
        page(ident, 0, 0, 0x02)
        + page(comment + setup, 0, 1, 0x00)
        + page(b"\x00" * 16, int(seconds * rate), 2, 0x04)
    )
    path.write_bytes(data)
    return path


# ── The reported scenario ───────────────────────────────────────────────────

class TestNoFfprobeNoMpv:
    def test_egg_duration_is_read_with_no_external_tools(
        self, tmp_path, no_external_tools, prompts
    ):
        """The whole point of the pure-Python reader."""
        egg = ogg_vorbis(tmp_path / "song.egg")

        assert au.get_audio_duration(egg) == pytest.approx(3.0, abs=0.05)
        assert prompts == [], "no prompt should appear when the reader works"

    def test_ogg_extension_works_the_same(self, tmp_path, no_external_tools, prompts):
        ogg = ogg_vorbis(tmp_path / "song.ogg", seconds=5.0)

        assert au.get_audio_duration(ogg) == pytest.approx(5.0, abs=0.05)
        assert prompts == []


class TestMissingReaderIsNotAnFfmpegProblem:
    def test_absent_mutagen_does_not_blame_ffmpeg(
        self, tmp_path, no_external_tools, prompts, no_mutagen
    ):
        egg = ogg_vorbis(tmp_path / "song.egg")
        no_mutagen("mutagen")

        assert au.get_audio_duration(egg) is None
        assert len(prompts) == 1
        assert prompts[0].reader_available is False

    def test_partially_bundled_mutagen_is_reported_the_same(
        self, tmp_path, no_external_tools, prompts, no_mutagen
    ):
        """A frozen build missing one format module used to break every read."""
        egg = ogg_vorbis(tmp_path / "song.egg")
        no_mutagen("mutagen.wave")

        assert au.get_audio_duration(egg) is None
        assert len(prompts) == 1
        assert prompts[0].reader_available is False

    def test_reader_available_reflects_the_import(self, no_mutagen):
        assert au.reader_available() is True

        au._reader_options = None
        au._reader_unavailable = None
        no_mutagen("mutagen.mp3")
        assert au.reader_available() is False

    def test_import_failure_is_only_attempted_once(self, no_mutagen, monkeypatch):
        no_mutagen("mutagen")
        assert au.reader_available() is False

        # A later call must not re-import; the recorded failure stands.
        def explode(*a, **kw):
            raise AssertionError("re-imported after a recorded failure")

        monkeypatch.setattr(builtins, "__import__", explode)
        assert au.reader_available() is False


class TestScopedFormatOptions:
    def test_only_the_documented_formats_are_imported(self):
        """Guards the fix: File(options=None) pulls in 26 format modules, so a
        single un-bundled one breaks every read in a frozen build."""
        names = [cls.__name__ for cls in au._reader_formats()]

        assert names == ["OggVorbis", "MP3", "WAVE", "MP4"]

    def test_options_match_the_pyinstaller_hiddenimports(self):
        """These must stay in step with Browser.spec / Browser.linux.spec."""
        root = Path(__file__).resolve().parent.parent
        declared = {
            line.strip().strip('",')
            for spec in ("Browser.spec", "Browser.linux.spec")
            for line in (root / spec).read_text(encoding="utf-8").splitlines()
            if "mutagen." in line
        }
        modules = {cls.__module__ for cls in au._reader_formats()}

        assert modules <= declared, f"not declared as hiddenimports: {modules - declared}"

    def test_file_is_called_with_explicit_options(self, tmp_path, monkeypatch):
        egg = ogg_vorbis(tmp_path / "song.egg")
        captured = {}

        import mutagen

        real_file = mutagen.File

        def spy(filething, options=None, **kw):
            captured["options"] = options
            return real_file(filething, options=options, **kw)

        monkeypatch.setattr(mutagen, "File", spy)
        au._reader_duration(egg)

        assert captured["options"] is not None
        assert len(captured["options"]) == 4


class TestUnparseableFilesStillPrompt:
    """A file the reader can't handle is the case ffprobe genuinely helps with,
    so the ffmpeg prompt is still correct there."""

    def test_truncated_file_reports_a_working_reader(
        self, tmp_path, no_external_tools, prompts
    ):
        bad = tmp_path / "truncated.egg"
        bad.write_bytes(ogg_vorbis(tmp_path / "src.egg").read_bytes()[:120])

        assert au.get_audio_duration(bad) is None
        assert len(prompts) == 1
        assert prompts[0].reader_available is True

    def test_empty_file_reports_a_working_reader(
        self, tmp_path, no_external_tools, prompts
    ):
        empty = tmp_path / "empty.egg"
        empty.write_bytes(b"")

        assert au.get_audio_duration(empty) is None
        assert prompts[0].reader_available is True

    def test_ffmpeg_presence_is_passed_through(
        self, tmp_path, no_external_tools, prompts, monkeypatch
    ):
        """Drives the choice between the repair and the install dialog."""
        monkeypatch.setattr(au, "find_ffmpeg", lambda: "/somewhere/ffmpeg")
        empty = tmp_path / "empty.egg"
        empty.write_bytes(b"")

        au.get_audio_duration(empty)

        assert prompts[0].ffmpeg_found is True
        assert prompts[0].ffprobe_found is False


class TestPromptSuppression:
    def test_prompt_fires_at_most_once_per_run(
        self, tmp_path, no_external_tools, prompts
    ):
        empty = tmp_path / "empty.egg"
        empty.write_bytes(b"")

        for _ in range(3):
            au.get_audio_duration(empty)

        assert len(prompts) == 1

    def test_present_ffprobe_suppresses_the_prompt(self, tmp_path, prompts, monkeypatch):
        """The chain isn't tool-starved, so there's nothing to offer."""
        monkeypatch.setattr(au, "find_ffprobe", lambda: "/usr/bin/ffprobe")
        monkeypatch.setattr(au, "_ffprobe_duration", lambda p: None)
        fake = types.ModuleType("libraries.mpv_backend")
        fake.load_mpv = lambda: None
        monkeypatch.setitem(sys.modules, "libraries.mpv_backend", fake)

        empty = tmp_path / "empty.egg"
        empty.write_bytes(b"")
        assert au.get_audio_duration(empty) is None
        assert prompts == []

    def test_missing_reader_prompts_even_when_ffprobe_exists(
        self, tmp_path, prompts, monkeypatch, no_mutagen
    ):
        """A broken reader is worth reporting regardless of the fallbacks —
        it's an install problem the user can't otherwise diagnose."""
        monkeypatch.setattr(au, "find_ffprobe", lambda: "/usr/bin/ffprobe")
        monkeypatch.setattr(au, "_ffprobe_duration", lambda p: None)
        fake = types.ModuleType("libraries.mpv_backend")
        fake.load_mpv = lambda: None
        monkeypatch.setitem(sys.modules, "libraries.mpv_backend", fake)
        no_mutagen("mutagen")

        empty = tmp_path / "song.egg"
        empty.write_bytes(b"")
        assert au.get_audio_duration(empty) is None
        assert len(prompts) == 1
        assert prompts[0].reader_available is False


class TestFallbackOrdering:
    def test_ffprobe_used_when_the_reader_cannot_parse(self, tmp_path, monkeypatch):
        monkeypatch.setattr(au, "_ffprobe_duration", lambda p: 12.5)
        empty = tmp_path / "empty.egg"
        empty.write_bytes(b"")

        assert au.get_audio_duration(empty) == 12.5

    def test_reader_wins_over_ffprobe(self, tmp_path, monkeypatch):
        """The reader is the primary path — no subprocess for the common case."""
        monkeypatch.setattr(
            au, "_ffprobe_duration",
            lambda p: pytest.fail("ffprobe called though the reader succeeded"),
        )
        egg = ogg_vorbis(tmp_path / "song.egg", seconds=4.0)

        assert au.get_audio_duration(egg) == pytest.approx(4.0, abs=0.05)

    def test_mpv_is_the_last_resort(self, tmp_path, monkeypatch):
        monkeypatch.setattr(au, "_ffprobe_duration", lambda p: None)
        monkeypatch.setattr(au, "_mpv_duration", lambda p: 7.25)
        empty = tmp_path / "empty.egg"
        empty.write_bytes(b"")

        assert au.get_audio_duration(empty) == 7.25
