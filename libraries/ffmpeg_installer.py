"""Offer to download ffmpeg when audio_utils.find_ffmpeg() comes up empty.

The repo doesn't ship the binaries (ffmpeg.exe alone is ~100MB and gitignored,
same as libmpv-2.dll), so a fresh checkout has no ffmpeg until someone places
one next to the app. This fetches it: the latest static win64/winarm64 GPL
build from BtbN/FFmpeg-Builds — the prebuilt package used everywhere for
Windows ffmpeg.

Unlike libmpv (see mpv_installer.py), these assets are plain .zip archives,
so extraction is pure stdlib ``zipfile`` — no external 7-Zip needed. The three
executables live under ``<name>/bin/`` inside the archive; they're flattened
out next to the app. The builds are fully static, so there's no VC-runtime
dependency to trip over.

Progress is reported the same way every other background download in this app
reports it: plain status-bar text via a caller-supplied ``status_cb``, no
separate progress window. Only the standard library is used for networking
(``urllib``), matching beatsaver_api.py and mpv_installer.py.
"""

from __future__ import annotations

import json
import os
import platform
import tarfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import NamedTuple

from libraries import dialogs
from libraries import platform_utils

RELEASES_API = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/tags/latest"
USER_AGENT = "BeatSaberSongManager/1.0 (github.com/adamboy8888/Beatsaber-Song-Manager)"

# The executables we pull out of the archive's bin/ directory (extension-less
# on Linux/macOS, .exe on Windows).
_WANTED = tuple(platform_utils.exe_name(n) for n in ("ffmpeg", "ffprobe", "ffplay"))

_META_TIMEOUT = 30
_ARCHIVE_TIMEOUT = 600
_MAX_RETRIES = 3
_CHUNK = 1 << 16  # 64 KiB

_PART_SUFFIX = ".part"
_SUPERSEDED_PREFIX = ".old-"

# Ask at most once per run — every subsequent failure should just fall back
# quietly rather than re-nag with the same dialog.
_offered = False


class FfmpegInstallError(Exception):
    """Raised for any recoverable failure fetching the ffmpeg archive."""


def target_arch() -> str:
    """The BtbN asset arch tag matching this machine.

    BtbN publishes Windows (win64/winarm64) and Linux (linux64/linuxarm64)
    static builds under the same 'latest' release.
    """
    is_arm = platform.machine().lower() in ("arm64", "aarch64")
    if platform_utils.IS_WINDOWS:
        return "winarm64" if is_arm else "win64"
    return "linuxarm64" if is_arm else "linux64"


def _asset_ext() -> str:
    """Archive extension for this platform's BtbN asset."""
    return "zip" if platform_utils.IS_WINDOWS else "tar.xz"


def _open(url: str, timeout: int, accept_json: bool = False):
    headers = {"User-Agent": USER_AGENT}
    if accept_json:
        headers["Accept"] = "application/vnd.github+json"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)


def find_asset(arch: str | None = None) -> tuple[str, str]:
    """Return ``(download_url, asset_name)`` for the latest static GPL build.

    Matches the un-versioned master build (``ffmpeg-master-latest-<arch>-gpl.zip``),
    not the ``-shared`` variant (needs side-by-side DLLs) or the version-pinned
    ``ffmpeg-n8.1-*`` assets.
    """
    arch = arch or target_arch()
    try:
        with _open(RELEASES_API, _META_TIMEOUT, accept_json=True) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raise FfmpegInstallError(f"HTTP {e.code} listing FFmpeg-Builds releases")
    except (urllib.error.URLError, TimeoutError) as e:
        raise FfmpegInstallError(f"network error listing ffmpeg releases: {e}")
    except json.JSONDecodeError as e:
        raise FfmpegInstallError(f"bad JSON from GitHub releases API: {e}")

    wanted = f"ffmpeg-master-latest-{arch}-gpl.{_asset_ext()}"
    for asset in data.get("assets", []) or []:
        if asset.get("name") == wanted:
            url = asset.get("browser_download_url")
            if url:
                return url, wanted
    raise FfmpegInstallError(f"no {wanted} asset in the latest FFmpeg-Builds release")


def _download(url: str, dest: Path, progress_cb=None) -> None:
    last_err: Exception | None = None
    for _ in range(_MAX_RETRIES):
        try:
            with _open(url, _ARCHIVE_TIMEOUT) as resp:
                total = int(resp.getheader("Content-Length") or 0)
                got = 0
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(_CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        if progress_cb:
                            progress_cb(got, total)
            return
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            last_err = e
            time.sleep(1)
    raise FfmpegInstallError(f"download failed: {last_err}")


class ExtractResult(NamedTuple):
    """Outcome of installing the executables from an archive.

    ``written`` lists the executable names now in place. ``deferred`` lists the
    ones whose previous copy was still running at swap time: the new binary is
    installed and will be used by the next invocation, but any process already
    running keeps the superseded copy open until it exits.
    """

    written: list[str]
    deferred: list[str]


def in_use(path: Path) -> bool:
    """True if ``path`` exists and can't currently be opened for writing.

    Windows opens a running executable with FILE_SHARE_READ only, so a write
    open fails while it runs — that's what makes an in-place overwrite blow up.
    POSIX has no such lock (and swapping a running binary by rename is safe
    there anyway), so this is only ever True on Windows.
    """
    if not path.exists():
        return False
    try:
        with open(path, "r+b"):
            return False
    except OSError:
        return True


def binaries_in_use(dest_dir: Path) -> list[str]:
    """Which of the target executables in ``dest_dir`` are currently running.

    Callers can use this to warn up front; the install itself no longer needs
    them idle (see ``_swap_into_place``).
    """
    return [base for base in _WANTED if in_use(dest_dir / base)]


def _is_leftover(name: str) -> bool:
    """True for our own ``<exe>.part`` / ``<exe>.old-<n>`` scratch files.

    Deliberately anchored to the three executable names: ``dest_dir`` is the
    shared app-data folder, which also holds the config, the song-hash cache and
    libmpv, and none of those should ever be in scope for deletion.
    """
    for base in _WANTED:
        if not name.startswith(base):
            continue
        rest = name[len(base):]
        if rest == _PART_SUFFIX or rest.startswith(_SUPERSEDED_PREFIX):
            return True
    return False


def _clear_superseded(dest_dir: Path) -> None:
    """Delete leftovers from an earlier install whose targets were in use.

    They stay behind because Windows won't unlink a running executable. By the
    time we're called again those processes are almost always gone, so this is
    the natural place to reap them; failures are ignored and retried next run.
    """
    try:
        entries = list(dest_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        if _is_leftover(entry.name):
            try:
                entry.unlink()
            except OSError:
                pass  # still locked, or gone already — try again next install


def _swap_into_place(staged: dict[str, Path], dest_dir: Path) -> list[str]:
    """Move freshly-extracted binaries over their live counterparts.

    ``os.replace`` is atomic per file but fails on Windows when the target is a
    running executable, which is what used to break this installer: a playing
    song means ffplay (and, with the visualizer open, ffmpeg) is live, so
    reinstalling to recover a missing ffprobe died partway through — after
    clobbering some binaries and before writing the one being repaired.

    Windows does allow *renaming* a running executable, so this runs in three
    phases. Every existing binary is moved aside first, then the new ones are
    dropped in, then the aside copies are deleted. A process still holding one
    keeps the renamed copy open and plays on undisturbed; that leftover survives
    phase three and is reaped by ``_clear_superseded`` on the next install.

    Moving *every* original aside (not just the locked ones) is what makes the
    set recoverable: any failure rolls the whole group back, so a half-updated
    mix of old and new binaries is never left on disk.

    Returns the names whose previous copy was still in use.
    """
    moved: list[tuple[str, Path, Path]] = []  # (base, target, aside)
    placed: list[Path] = []
    deferred: list[str] = []

    def rollback() -> None:
        for target in placed:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        for _base, target, aside in reversed(moved):
            try:
                os.replace(aside, target)
            except OSError:
                pass  # best effort; _clear_superseded picks up the remains

    try:
        # Phase 1 — move the current binaries out of the way, noting which ones
        # a running process still has open.
        for base in staged:
            target = dest_dir / base
            if not target.exists():
                continue
            was_live = in_use(target)
            aside = dest_dir / f"{base}{_SUPERSEDED_PREFIX}{time.time_ns()}"
            os.replace(target, aside)
            moved.append((base, target, aside))
            if was_live:
                deferred.append(base)

        # Phase 2 — put the new ones in place.
        for base, part in staged.items():
            target = dest_dir / base
            os.replace(part, target)
            placed.append(target)
    except OSError as e:
        rollback()
        raise FfmpegInstallError(f"could not install binaries: {e}")

    # Phase 3 — drop the superseded copies nothing is using. The in-use ones
    # can't be unlinked on Windows and are left for _clear_superseded.
    for base, _target, aside in moved:
        if base in deferred:
            continue
        try:
            aside.unlink()
        except OSError:
            pass

    return deferred


def _extract_exes(archive: Path, dest_dir: Path) -> ExtractResult:
    """Extract bin/{ffmpeg,ffprobe,ffplay} from ``archive`` into ``dest_dir``.

    BtbN archives nest everything under a top-level ``<name>/`` folder, so
    entries are matched by basename and written flat into ``dest_dir``. Windows
    assets are ``.zip``; Linux assets are ``.tar.xz``.

    Everything is extracted to ``.part`` files first and only swapped over the
    live binaries once all of them are on disk, so a mid-extraction failure
    can't leave a mix of old and new. On Unix the execute bit is set.
    """
    staged: dict[str, Path] = {}

    def part_for(base: str) -> Path:
        return dest_dir / f"{base}{_PART_SUFFIX}"

    def drain(src, base: str) -> None:
        part = part_for(base)
        with open(part, "wb") as out:
            while True:
                chunk = src.read(_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
        staged[base] = part

    _clear_superseded(dest_dir)
    try:
        if archive.name.endswith((".tar.xz", ".tar.gz", ".tar.bz2")):
            with tarfile.open(archive) as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    base = member.name.rsplit("/", 1)[-1]
                    if base in _WANTED and "/bin/" in f"/{member.name}":
                        src = tf.extractfile(member)
                        if src is None:
                            continue
                        with src:
                            drain(src, base)
        else:
            with zipfile.ZipFile(archive) as zf:
                for info in zf.infolist():
                    base = info.filename.rsplit("/", 1)[-1]
                    if base in _WANTED and "/bin/" in f"/{info.filename}":
                        with zf.open(info) as src:
                            drain(src, base)
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as e:
        for part in staged.values():
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
        raise FfmpegInstallError(f"could not extract archive: {e}")

    if not platform_utils.IS_WINDOWS:
        for part in staged.values():
            try:
                os.chmod(part, 0o755)
            except OSError:
                pass

    deferred = _swap_into_place(staged, dest_dir)
    return ExtractResult(list(staged), deferred)


def offer_download_once(dest_dir: Path, dispatch_fn, status_cb=None,
                        on_unavailable=None, on_ready=None) -> None:
    """Ask the user, at most once per run, whether to fetch ffmpeg now.

    Downloads the matching static BtbN build into ``dest_dir`` and extracts the
    ffmpeg/ffprobe/ffplay executables from it in a background thread, reporting
    progress through ``status_cb`` (a plain ``str -> None`` callable, e.g.
    ``lambda text: status_bar.config(text=text)``). ``dispatch_fn`` (a
    thread-safe dispatcher) marshals status/completion callbacks from the worker
    thread back onto the main thread.

    ``on_unavailable`` is the caller's fallback for "ffmpeg still isn't usable"
    — it fires when the offer was already resolved earlier this run, the user
    declines, or the download/extraction fails. ``on_ready`` fires once the
    binaries are in place (audio_utils.find_ffmpeg re-probes on every miss, so
    they're picked up live, no restart); if omitted, an info dialog is shown.
    """
    global _offered

    def unavailable() -> None:
        if on_unavailable is not None:
            on_unavailable()

    def report(text: str) -> None:
        if status_cb is None:
            return
        try:
            dispatch_fn(lambda: status_cb(text))
        except Exception:
            pass  # UI already torn down (e.g. app closing)

    if _offered:
        unavailable()
        return
    _offered = True

    arch = target_arch()
    if not dialogs.ask_yes_no(
        "ffmpeg Not Found",
        "ffmpeg wasn't found in the app-data folder, on your PATH, or next to "
        "the app. It's used for the visualizer's sound bars and audio "
        "conversion. Music playback and Cinema videos work through libmpv "
        "without it.\n\n"
        f"Download the latest static ffmpeg ({arch}) build from "
        "github.com/BtbN/FFmpeg-Builds and install it to the app-data folder now?",
    ):
        unavailable()
        return

    result: dict = {}

    def on_progress(got: int, total: int) -> None:
        if total:
            report(f"Downloading ffmpeg… {int(got * 100 / total)}%")
        else:
            report(f"Downloading ffmpeg… {got // (1 << 20)} MB")

    def worker() -> None:
        try:
            report("Locating latest ffmpeg build…")
            url, name = find_asset(arch)
            archive_path = dest_dir / name
            report(f"Downloading {name}…")
            _download(url, archive_path, on_progress)

            report("Extracting ffmpeg…")
            extracted = _extract_exes(archive_path, dest_dir)
            try:
                archive_path.unlink()
            except OSError:
                pass
            ffmpeg_bin = platform_utils.exe_name("ffmpeg")
            if ffmpeg_bin not in extracted.written:
                raise FfmpegInstallError(f"archive did not contain {ffmpeg_bin}")
            result["written"] = extracted.written
            result["deferred"] = extracted.deferred
        except FfmpegInstallError as e:
            result["error"] = str(e)
        try:
            dispatch_fn(_finish)
        except Exception:
            pass  # UI already torn down (e.g. app closing)

    def _finish() -> None:
        if "error" in result:
            report(f"ffmpeg download failed: {result['error']}")
            dialogs.show_error("Download Failed", f"Couldn't download ffmpeg:\n{result['error']}")
            unavailable()
            return

        deferred = result.get("deferred") or []
        report("ffmpeg installed.")
        if on_ready is not None:
            on_ready()
        elif deferred:
            dialogs.show_info(
                "ffmpeg Installed",
                "ffmpeg was installed. "
                f"{', '.join(sorted(deferred))} was in use by playback, so the "
                "running copy stays active until playback stops — anything "
                "started after that uses the new build.",
            )
        else:
            dialogs.show_info(
                "ffmpeg Installed",
                "ffmpeg was installed — the visualizer's sound bars and audio "
                "conversion are now available.",
            )

    threading.Thread(target=worker, daemon=True).start()
