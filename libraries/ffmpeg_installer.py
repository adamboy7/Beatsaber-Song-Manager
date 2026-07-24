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


def _extract_exes(archive: Path, dest_dir: Path) -> list[str]:
    """Extract bin/{ffmpeg,ffprobe,ffplay} from ``archive`` into ``dest_dir``.

    BtbN archives nest everything under a top-level ``<name>/`` folder, so
    entries are matched by basename and written flat into ``dest_dir``. Windows
    assets are ``.zip``; Linux assets are ``.tar.xz``. Returns the list of
    executable names successfully written; on Unix the execute bit is set.
    """
    written: list[str] = []
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
                        with src, open(dest_dir / base, "wb") as out:
                            while True:
                                chunk = src.read(_CHUNK)
                                if not chunk:
                                    break
                                out.write(chunk)
                        written.append(base)
        else:
            with zipfile.ZipFile(archive) as zf:
                for info in zf.infolist():
                    base = info.filename.rsplit("/", 1)[-1]
                    if base in _WANTED and "/bin/" in f"/{info.filename}":
                        with zf.open(info) as src, open(dest_dir / base, "wb") as out:
                            while True:
                                chunk = src.read(_CHUNK)
                                if not chunk:
                                    break
                                out.write(chunk)
                        written.append(base)
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as e:
        raise FfmpegInstallError(f"could not extract archive: {e}")

    if not platform_utils.IS_WINDOWS:
        for base in written:
            try:
                os.chmod(dest_dir / base, 0o755)
            except OSError:
                pass
    return written


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
        "ffmpeg wasn't found next to the app or on your PATH, so audio "
        "conversion is unavailable.\n\n"
        f"Download the latest static ffmpeg ({arch}) build from "
        "github.com/BtbN/FFmpeg-Builds and install it now?",
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
            written = _extract_exes(archive_path, dest_dir)
            try:
                archive_path.unlink()
            except OSError:
                pass
            ffmpeg_bin = platform_utils.exe_name("ffmpeg")
            if ffmpeg_bin not in written:
                raise FfmpegInstallError(f"archive did not contain {ffmpeg_bin}")
            result["written"] = written
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

        report("ffmpeg installed.")
        if on_ready is not None:
            on_ready()
        else:
            dialogs.show_info(
                "ffmpeg Installed",
                "ffmpeg was installed next to the app — audio conversion is "
                "now available.",
            )

    threading.Thread(target=worker, daemon=True).start()
