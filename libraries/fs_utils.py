"""Small filesystem helpers shared across the codebase."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Write ``content`` to ``path`` atomically (mkstemp + os.replace).

    On failure the temp file is removed and the exception re-raised; the
    destination file is left untouched either way.
    """
    target = Path(path)
    fd, tmp_str = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        os.replace(tmp_str, target)
    except Exception:
        Path(tmp_str).unlink(missing_ok=True)
        raise
