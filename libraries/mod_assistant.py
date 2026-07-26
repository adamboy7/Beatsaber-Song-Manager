"""Detect a local Mod Assistant install via its registered URL protocols.

Mod Assistant (https://github.com/bsmg/ModAssistant) is a Windows-only tool
that registers custom URL protocol handlers when installed, so one-click
`beatsaver://` / `bsplaylist://` links launch it. We piggy-back on those
handlers to detect it: if the protocol is registered on the system, its
registry entry also carries the path to the Mod Assistant executable that
Windows invokes for the link.

There's no protocol handler on Linux (Mod Assistant has no native Linux
build), so detection there always fails and callers should keep opening the
one-click link in the browser instead.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from libraries import platform_utils

# The protocols Mod Assistant registers. beatsaver:// is the primary one; the
# playlist handler is checked as a fallback in case only it is registered.
_PROTOCOLS = ("beatsaver", "bsplaylist")


def _command_executable(command: str) -> Path | None:
    """Extract the executable path from a shell 'open command' string.

    Handlers look like ``"C:\\path\\ModAssistant.exe" "%1"`` (quoted) or
    ``C:\\path\\ModAssistant.exe %1`` (bare). Pull out the first token, drop
    the ``%1`` placeholder, and return it only if the file actually exists.
    """
    command = command.strip()
    if not command:
        return None

    if command.startswith('"'):
        end = command.find('"', 1)
        exe = command[1:end] if end != -1 else command[1:]
    else:
        exe = command.split(" %", 1)[0].split(" ", 1)[0]

    exe = exe.strip().strip('"')
    if not exe:
        return None

    path = Path(exe)
    return path if path.is_file() else None


def _protocol_command(protocol: str) -> str | None:
    """Return the raw ``shell\\open\\command`` string for a URL protocol, if any.

    Checks the per-user classes first (where Mod Assistant registers by
    default without admin rights), then the machine-wide classes.
    """
    try:
        import winreg
    except ImportError:
        return None

    subkey = rf"{protocol}\shell\open\command"
    for root, base in (
        (winreg.HKEY_CURRENT_USER, r"Software\Classes"),
        (winreg.HKEY_CLASSES_ROOT, ""),
    ):
        full = rf"{base}\{subkey}" if base else subkey
        try:
            with winreg.OpenKey(root, full) as key:
                value, _ = winreg.QueryValueEx(key, "")
                if value:
                    return value
        except OSError:
            continue
    return None


@lru_cache(maxsize=1)
def find_mod_assistant() -> Path | None:
    """Return the path to a detected Mod Assistant executable, or None.

    Windows-only: reads the registered protocol handlers and returns the
    executable they point at when it exists on disk. Returns None on every
    other platform (and whenever the protocol isn't registered), so callers
    fall back to opening the link in a browser.

    Cached because it touches the registry/filesystem and is called on every
    Tools-menu open; call :func:`invalidate_cache` after anything that could
    change the install state.
    """
    if not platform_utils.IS_WINDOWS:
        return None

    for protocol in _PROTOCOLS:
        command = _protocol_command(protocol)
        if not command:
            continue
        exe = _command_executable(command)
        if exe is not None and _looks_like_mod_assistant(exe):
            return exe
    return None


def _looks_like_mod_assistant(exe: Path) -> bool:
    """Guard against unrelated apps that happen to own the protocol.

    Only accept handlers whose executable name looks like Mod Assistant, so a
    different beatsaver:// handler doesn't get mislabelled. Matches
    ``ModAssistant.exe`` regardless of case or surrounding spaces.
    """
    return re.search(r"mod\s*assistant", exe.stem, re.IGNORECASE) is not None


def invalidate_cache() -> None:
    """Clear the memoized detection result (e.g. after an install)."""
    find_mod_assistant.cache_clear()
