import re
from pathlib import Path

from libraries import platform_utils
from libraries.constants import STEAM_RELATIVE_PATH


def parse_vdf_library_paths(vdf_path: Path) -> list[Path]:
    """Return every Steam library root found in libraryfolders.vdf."""
    paths = []
    try:
        text = vdf_path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r'"path"\s+"([^"]+)"', text):
            p = Path(match.group(1).replace("\\\\", "\\"))
            if p.exists():
                paths.append(p)
    except FileNotFoundError:
        pass
    return paths


def get_steam_path_from_registry() -> Path | None:
    """Read Steam's install path from HKEY_CURRENT_USER\\Software\\Valve\\Steam."""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
        steam_path, _ = winreg.QueryValueEx(key, "SteamPath")
        winreg.CloseKey(key)
        return Path(steam_path)
    except Exception:
        return None


def _vdf_candidates(vdf_path: Path | None = None) -> list[Path]:
    """Ordered list of libraryfolders.vdf files to probe.

    Starts with an explicit override, then Steam's registry-reported location
    (Windows only), then the platform's default install/data dirs (including
    Linux native and Flatpak).
    """
    if vdf_path is not None:
        return [vdf_path]

    candidates: list[Path] = []
    steam_root = get_steam_path_from_registry()
    if steam_root:
        candidates.append(steam_root / "steamapps" / "libraryfolders.vdf")
    for default in platform_utils.steam_library_vdf_candidates():
        if default not in candidates:
            candidates.append(default)
    return candidates


def steam_library_roots(vdf_path: Path | None = None) -> list[Path]:
    """Every Steam library root discoverable on this machine."""
    roots: list[Path] = []
    for candidate_vdf in _vdf_candidates(vdf_path):
        for root in parse_vdf_library_paths(candidate_vdf):
            if root not in roots:
                roots.append(root)
    return roots


def find_beatsaber_custom_levels(vdf_path: Path | None = None) -> Path | None:
    """Locate the CustomLevels folder by scanning Steam library folders.

    ``STEAM_RELATIVE_PATH`` is the same under Proton — the game files live at
    that relative path inside the library root regardless of platform — so this
    works on Linux once the Linux VDF candidates are in play.
    """
    for root in steam_library_roots(vdf_path):
        candidate = root / STEAM_RELATIVE_PATH
        if candidate.is_dir():
            return candidate
    return None
