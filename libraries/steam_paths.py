import re
from pathlib import Path

from libraries.constants import DEFAULT_VDF_PATH, STEAM_RELATIVE_PATH


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


def find_beatsaber_custom_levels(vdf_path: Path | None = None) -> Path | None:
    """Locate the CustomLevels folder by scanning Steam library folders."""
    if vdf_path is not None:
        candidates = [vdf_path]
    else:
        candidates = []
        steam_root = get_steam_path_from_registry()
        if steam_root:
            candidates.append(steam_root / "steamapps" / "libraryfolders.vdf")
        if DEFAULT_VDF_PATH not in candidates:
            candidates.append(DEFAULT_VDF_PATH)

    for candidate_vdf in candidates:
        for root in parse_vdf_library_paths(candidate_vdf):
            candidate = root / STEAM_RELATIVE_PATH
            if candidate.is_dir():
                return candidate
    return None
