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


def find_beatsaber_custom_levels(vdf_path: Path = DEFAULT_VDF_PATH) -> Path | None:
    """Locate the CustomLevels folder by scanning Steam library folders."""
    library_roots = parse_vdf_library_paths(vdf_path)
    for root in library_roots:
        candidate = root / STEAM_RELATIVE_PATH
        if candidate.is_dir():
            return candidate
    return None
