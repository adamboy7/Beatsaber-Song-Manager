"""Offer to download libmpv when mpv_backend.find_libmpv() comes up empty.

Nothing ships the DLL in the repo (it's ~110MB and gitignored, same as
ffmpeg.exe), so a fresh checkout has no libmpv until someone places one next
to the app. This fetches it: the latest mpv-dev-<arch>.7z from
shinchiro/mpv-winbuild-cmake — the prebuilt package used everywhere else that
bundles libmpv for Windows — matching the running Python's 32/64-bitness.

Those archives compress the DLL with 7-Zip's BCJ2 filter. No pure-Python 7z
reader decodes it (py7zr explicitly declines BCJ2 rather than get it wrong),
so extraction shells out to a system 7z/7za if one is on PATH or installed in
the usual place. 7-Zip is common enough among Beat Saber modders that this
covers most cases; if none is found, the archive is still downloaded and the
user is told how to finish the extraction by hand.

Progress is reported the same way every other background download in this
app reports it (yt-dlp's own download, then Cinema video downloads in
browser_actions.py): plain status-bar text via a caller-supplied ``status_cb``,
no separate progress window. Only the standard library is used for
networking (``urllib``), matching beatsaver_api.py.
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from libraries import dialogs
from libraries import platform_utils

RELEASES_API = "https://api.github.com/repos/shinchiro/mpv-winbuild-cmake/releases/latest"
USER_AGENT = "BeatSaberSongManager/1.0 (github.com/adamboy8888/Beatsaber-Song-Manager)"

_META_TIMEOUT = 30
_ARCHIVE_TIMEOUT = 300
_MAX_RETRIES = 3
_CHUNK = 1 << 16  # 64 KiB

# Ask at most once per run — every subsequent playback failure should just
# fall back quietly rather than re-nag with the same dialog.
_offered = False


class MpvInstallError(Exception):
    """Raised for any recoverable failure fetching the mpv-dev archive."""


def target_arch() -> str:
    """The mpv-dev asset arch tag matching this Python's bitness."""
    return "x86_64" if struct.calcsize("P") == 8 else "i686"


def _open(url: str, timeout: int, accept_json: bool = False):
    headers = {"User-Agent": USER_AGENT}
    if accept_json:
        headers["Accept"] = "application/vnd.github+json"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)


def find_asset(arch: str | None = None) -> tuple[str, str]:
    """Return ``(download_url, asset_name)`` for the latest mpv-dev-<arch> build."""
    arch = arch or target_arch()
    try:
        with _open(RELEASES_API, _META_TIMEOUT, accept_json=True) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raise MpvInstallError(f"HTTP {e.code} listing mpv-winbuild-cmake releases")
    except (urllib.error.URLError, TimeoutError) as e:
        raise MpvInstallError(f"network error listing mpv releases: {e}")
    except json.JSONDecodeError as e:
        raise MpvInstallError(f"bad JSON from GitHub releases API: {e}")

    prefix = f"mpv-dev-{arch}-"
    for asset in data.get("assets", []) or []:
        name = asset.get("name", "")
        if name.startswith(prefix) and name.endswith(".7z"):
            url = asset.get("browser_download_url")
            if url:
                return url, name
    raise MpvInstallError(
        f"no mpv-dev-{arch}-*.7z asset in the latest mpv-winbuild-cmake release"
    )


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
    raise MpvInstallError(f"download failed: {last_err}")


def find_7z_exe() -> str | None:
    """Locate a 7-Zip executable capable of extracting BCJ2-filtered archives."""
    for name in ("7z", "7za", "7zr"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in (
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ):
        if Path(candidate).exists():
            return candidate
    return None


def _extract_dll(seven_zip: str, archive: Path, dest_dir: Path) -> bool:
    """Extract just libmpv-2.dll from ``archive`` into ``dest_dir``. Returns success."""
    try:
        result = subprocess.run(
            [seven_zip, "e", str(archive), f"-o{dest_dir}", "libmpv-2.dll", "-y"],
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and (dest_dir / "libmpv-2.dll").exists()


def offer_download_once(dest_dir: Path, dispatch_fn, status_cb=None,
                        on_unavailable=None, on_ready=None) -> None:
    """Ask the user, at most once per run, whether to fetch libmpv now.

    Downloads the matching-architecture mpv-dev archive into ``dest_dir`` and
    tries to extract libmpv-2.dll from it in a background thread, reporting
    progress through ``status_cb`` (a plain ``str -> None`` callable, e.g.
    ``lambda text: status_bar.config(text=text)``) — the same status-bar
    convention yt-dlp's own download and Cinema video downloads use, rather
    than a separate progress window. ``dispatch_fn`` (a thread-safe dispatcher,
    see ``libraries.tk_dispatch``) marshals status/completion callbacks from
    the worker thread back onto the main thread.

    ``on_unavailable`` is the caller's fallback for "mpv still isn't usable
    this session" — e.g. showing its own "Play Audio" warning. It fires
    exactly on the paths where that's true: the offer was already made and
    resolved earlier this run, the user declines the offer, or the download or
    extraction fails (including a DLL that installs but still won't load).

    ``on_ready`` fires when the freshly-installed libmpv loads successfully —
    load_mpv() picks up the new DLL live, no restart needed — so the caller
    can retry whatever playback prompted the offer. If it's omitted, a plain
    "installed and ready" info dialog is shown instead.
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

    # The shinchiro mpv-dev builds this fetches (and the 7-Zip BCJ2 extraction)
    # are Windows-only. There's no equivalent drop-in static libmpv for
    # Linux/macOS, so point the user at their package manager instead. load_mpv()
    # re-probes on every call, so a system install is picked up without restart.
    if not platform_utils.IS_WINDOWS:
        dialogs.show_info(
            "libmpv Not Found",
            "In-app audio/video playback needs libmpv, which wasn't found.\n\n"
            "Install it with your package manager, then try again:\n"
            "  • Debian/Ubuntu:  sudo apt install libmpv2\n"
            "  • Fedora:         sudo dnf install mpv-libs\n"
            "  • Arch:           sudo pacman -S mpv\n\n"
            "(On older distros the package may be named libmpv1.) "
            "No restart needed once it's installed.",
        )
        unavailable()
        return

    arch = target_arch()
    if not dialogs.ask_yes_no(
        "libmpv Not Found",
        "libmpv-2.dll wasn't found next to the app, so in-app audio/video "
        "playback is unavailable.\n\n"
        f"Download the latest mpv-dev ({arch}) build from "
        "github.com/shinchiro/mpv-winbuild-cmake and install libmpv-2.dll now?",
    ):
        unavailable()
        return

    result: dict = {}

    def on_progress(got: int, total: int) -> None:
        if total:
            pct = int(got * 100 / total)
            report(f"Downloading libmpv… {pct}%")
        else:
            report(f"Downloading libmpv… {got // (1 << 20)} MB")

    def worker() -> None:
        try:
            report("Locating latest mpv-dev build…")
            url, name = find_asset(arch)
            archive_path = dest_dir / name
            report(f"Downloading {name}…")
            _download(url, archive_path, on_progress)

            report(f"Extracting {name}…")
            seven_zip = find_7z_exe()
            extracted = bool(seven_zip) and _extract_dll(seven_zip, archive_path, dest_dir)
            if extracted:
                try:
                    archive_path.unlink()
                except OSError:
                    pass
            result["archive_path"] = archive_path
            result["extracted"] = extracted
        except MpvInstallError as e:
            result["error"] = str(e)
        try:
            dispatch_fn(_finish)
        except Exception:
            pass  # UI already torn down (e.g. app closing)

    def _finish() -> None:
        if "error" in result:
            report(f"libmpv download failed: {result['error']}")
            dialogs.show_error("Download Failed", f"Couldn't download libmpv:\n{result['error']}")
            unavailable()
            return

        archive_path: Path = result["archive_path"]
        if result["extracted"]:
            report("libmpv installed.")
            # Load the just-installed DLL live — load_mpv() only caches
            # successes, so this newly-present binary is picked up without a
            # restart. Import here (not at module top) to avoid a circular
            # import: mpv_backend has no dependency on this module.
            from libraries import mpv_backend
            if mpv_backend.load_mpv() is not None:
                report("libmpv installed and ready.")
                if on_ready is not None:
                    on_ready()
                else:
                    dialogs.show_info(
                        "libmpv Installed",
                        "libmpv-2.dll was installed and loaded — in-app "
                        "audio/video playback is now available.",
                    )
            else:
                # Present on disk but still won't load (e.g. arch mismatch or
                # missing VC runtime). A restart wouldn't fix that, so report
                # the real reason and fall back.
                report("libmpv installed but could not be loaded.")
                dialogs.show_error(
                    "libmpv Not Loaded",
                    mpv_backend.load_error()
                    or "libmpv-2.dll was installed but could not be loaded.",
                )
                unavailable()
        else:
            report(f"Saved {archive_path.name} — extract it manually to finish.")
            dialogs.show_info(
                "Archive Downloaded",
                f"Saved {archive_path.name} next to the app, but no 7-Zip install "
                "was found to auto-extract it (the archive uses a compression "
                "filter only 7-Zip supports).\n\n"
                "Open it with 7-Zip, extract libmpv-2.dll into this same folder, "
                "then try playback again — no restart needed.",
            )
            unavailable()

    threading.Thread(target=worker, daemon=True).start()
