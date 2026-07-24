"""Centralized OS-specific primitives.

Everything platform-dependent — binary naming, opening a folder in the file
manager, locating Steam libraries and the Beat Saber Proton prefix — lives here
so the rest of the codebase can stay OS-agnostic. The app targets Windows
natively and Linux via Steam Play/Proton (Beat Saber runs under Proton, so its
files live inside a Wine prefix; see ``beatsaber_prefix_dirs``).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# Beat Saber's Steam app id, reused for the Proton compatdata prefix.
BEATSABER_APP_ID = "620980"


def exe_name(base: str) -> str:
    """``'ffmpeg'`` -> ``'ffmpeg.exe'`` on Windows, ``'ffmpeg'`` elsewhere."""
    return f"{base}.exe" if IS_WINDOWS else base


def no_window_flags() -> int:
    """``subprocess`` creationflags to suppress a console window.

    ``CREATE_NO_WINDOW`` only exists on Windows; everywhere else this is 0, so
    callers can pass it unconditionally.
    """
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def open_in_file_manager(path: Path) -> None:
    """Reveal ``path`` in the platform's file manager. Best-effort."""
    try:
        if IS_WINDOWS:
            os.startfile(path)  # type: ignore[attr-defined]  # noqa
        elif IS_MAC:
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass  # no file manager / unsupported desktop — silently ignore


def steam_library_vdf_candidates() -> list[Path]:
    """Default ``libraryfolders.vdf`` locations to probe, most-likely first.

    On Windows this is just the standard Program Files install; Steam's real
    location is usually resolved from the registry elsewhere. On Linux it covers
    the native, alternate, and Flatpak Steam data dirs.
    """
    if IS_WINDOWS:
        return [Path(r"C:\Program Files (x86)\Steam\steamapps\libraryfolders.vdf")]
    home = Path.home()
    return [
        home / ".steam/steam/steamapps/libraryfolders.vdf",
        home / ".local/share/Steam/steamapps/libraryfolders.vdf",
        home / ".steam/root/steamapps/libraryfolders.vdf",
        home / ".var/app/com.valvesoftware.Steam/data/Steam/steamapps/libraryfolders.vdf",
    ]


def proton_prefix_appdata(library_root: Path) -> Path:
    """LocalLow AppData dir for Beat Saber inside its Proton prefix.

    Under Proton, Beat Saber writes to a Wine prefix at
    ``<library>/steamapps/compatdata/<appid>/pfx/drive_c/...``; the Windows
    ``AppData/LocalLow`` maps to ``users/steamuser/AppData/LocalLow`` there.
    """
    return (
        library_root
        / "steamapps/compatdata"
        / BEATSABER_APP_ID
        / "pfx/drive_c/users/steamuser/AppData/LocalLow"
    )
