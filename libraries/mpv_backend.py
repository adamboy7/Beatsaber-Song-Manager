"""
Shared libmpv loader.

Both the audio player (media_player.py) and the Cinema video backend
(visualizer_window.py) embed libmpv in-process via the python-mpv binding.
python-mpv locates the libmpv DLL through the MPV_DLL_PATH environment
variable (or PATH); this module points it at a DLL sitting next to the
application — the same "drop the binary next to the EXE" convention already
used for ffmpeg/ffprobe — before importing the binding.
"""

from __future__ import annotations

import ctypes
import functools
import os
import sys
from pathlib import Path

# Accepted DLL filenames, in preference order. libmpv-2.dll is what current
# mpv releases ship; the others cover older builds and manual renames.
_DLL_NAMES = ("libmpv-2.dll", "mpv-2.dll", "mpv-1.dll", "libmpv.dll")

LIBMPV_HINT = (
    "libmpv not found. Place libmpv-2.dll next to the application "
    "(same place as ffmpeg.exe) or add it to your PATH."
)

# Human-readable reason the last load_mpv() call returned None, for error
# dialogs — distinguishes "DLL missing" from "python-mpv not installed" from
# "DLL present but won't load" (e.g. 32/64-bit mismatch).
_load_error: str | None = None


def load_error() -> str | None:
    return _load_error


def _local_dir() -> Path:
    # When frozen by PyInstaller, look next to the EXE, not in the temp bundle.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent.parent


def find_libmpv() -> str | None:
    """Return the path to a local libmpv DLL, or None if not present."""
    for name in _DLL_NAMES:
        p = _local_dir() / name
        if p.exists():
            return str(p)
    return None


@functools.lru_cache(maxsize=1)
def load_mpv():
    """Import and return the python-mpv module, or None if unavailable.

    Unavailable means the python-mpv package isn't installed, no libmpv DLL
    could be found (locally or on PATH), or the DLL exists but won't load.
    ``load_error()`` reports which. The result is cached — like find_ffmpeg,
    fixing the problem requires an app restart to be picked up.
    """
    global _load_error
    local = find_libmpv()
    if local:
        # python-mpv honors MPV_DLL_PATH as an explicit override; don't
        # clobber one the user has already set themselves.
        os.environ.setdefault("MPV_DLL_PATH", local)
        dll_dir = str(Path(local).parent)
        try:
            os.add_dll_directory(dll_dir)
        except (OSError, AttributeError):
            pass
        # Also prepend to PATH: older python-mpv versions ignore MPV_DLL_PATH
        # and search PATH via ctypes.util.find_library instead.
        os.environ["PATH"] = dll_dir + os.pathsep + os.environ.get("PATH", "")
        # Validate the DLL actually loads before python-mpv tries — this
        # surfaces the real Windows error (bad arch, corrupt download,
        # missing VC runtime) instead of a generic import failure.
        try:
            ctypes.CDLL(local)
        except OSError as exc:
            _load_error = f"{Path(local).name} was found but failed to load: {exc}"
            return None
    try:
        import mpv
        return mpv
    except ModuleNotFoundError:
        _load_error = (
            "The Python package 'python-mpv' is not installed. "
            "Run: pip install python-mpv"
        )
        return None
    except Exception as exc:
        _load_error = f"python-mpv failed to initialize: {exc}"
        return None
