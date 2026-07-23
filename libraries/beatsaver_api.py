"""Direct BeatSaver map downloads.

Fetches map metadata from ``api.beatsaver.com``, downloads the map zip from the
BeatSaver CDN, and extracts it into ``CustomLevels/<key> (<title> - <author>)``.
Curation works even when Beat Saber itself isn't installed, as long as a
CustomLevels folder can be resolved. Only the standard library is used
(``urllib``, ``zipfile``, ``json``).
"""

from __future__ import annotations

import io
import json
import re
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

API_PREFIX = "https://api.beatsaver.com"

# BeatSaver asks API consumers to send a descriptive User-Agent identifying the
# application and version so they can contact maintainers about misbehaving
# clients. Bump the version here when releasing.
USER_AGENT = "BeatSaberSongManager/1.0 (github.com/adamboy8888/Beatsaber-Song-Manager)"

# Characters Windows and Beat Saber disallow in folder names. Stripping the
# same set keeps folder names consistent with what users already have on disk.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Network timeouts (seconds).
_META_TIMEOUT = 30
_ZIP_TIMEOUT = 120
_MAX_RETRIES = 3


class BeatSaverError(Exception):
    """Raised for any recoverable failure fetching or installing a map."""


# ── HTTP helpers ────────────────────────────────────────────────────────────

def _open(url: str, timeout: int, accept_json: bool = False):
    headers = {"User-Agent": USER_AGENT}
    if accept_json:
        headers["Accept"] = "application/json"
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def _ratelimit_wait(headers) -> None:
    """Sleep until the BeatSaver rate-limit window resets.

    ``Rate-Limit-Reset`` is a Unix timestamp (seconds). Cap the wait so a bad
    header value can't stall the UI thread's worker indefinitely.
    """
    reset = None
    if headers is not None:
        reset = headers.get("Rate-Limit-Reset") or headers.get("rate-limit-reset")
    try:
        wait = int(reset) - int(time.time())
    except (TypeError, ValueError):
        wait = 5
    time.sleep(max(1, min(wait, 60)))


# ── Map metadata ────────────────────────────────────────────────────────────

def fetch_map(id_or_hash: str, by: str = "key") -> dict:
    """Return the BeatSaver map object for a key (``by='key'``) or hash.

    Handles HTTP 429 by waiting for the advertised reset window and retrying.
    """
    segment = "/maps/hash/" if by == "hash" else "/maps/id/"
    url = f"{API_PREFIX}{segment}{id_or_hash}"

    for _ in range(_MAX_RETRIES):
        try:
            with _open(url, _META_TIMEOUT, accept_json=True) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _ratelimit_wait(e.headers)
                continue
            raise BeatSaverError(f"HTTP {e.code} fetching {id_or_hash}")
        except (urllib.error.URLError, TimeoutError) as e:
            raise BeatSaverError(f"network error fetching {id_or_hash}: {e}")
        except json.JSONDecodeError as e:
            raise BeatSaverError(f"bad JSON for {id_or_hash}: {e}")

        # The id endpoint returns the map directly. The hash endpoint returns
        # the map directly for a single hash, but historically could return a
        # {hash: map} dict — handle both.
        if isinstance(data, dict) and "versions" in data:
            return data
        if by == "hash" and isinstance(data, dict):
            want = id_or_hash.lower()
            for k, v in data.items():
                if k.lower() == want and isinstance(v, dict) and "versions" in v:
                    return v
            for v in data.values():
                if isinstance(v, dict) and "versions" in v:
                    return v
        raise BeatSaverError(f"unexpected response shape for {id_or_hash}")

    raise BeatSaverError(f"rate limited: gave up on {id_or_hash}")


def _pick_version(map_json: dict, want_hash: str | None = None) -> dict:
    versions = map_json.get("versions") or []
    if not versions:
        raise BeatSaverError("map has no downloadable versions")
    if want_hash:
        for v in versions:
            if (v.get("hash") or "").lower() == want_hash.lower():
                return v
    # Newest version wins. createdAt is an ISO-8601 string, so a lexical max is
    # equivalent to a chronological one.
    return max(versions, key=lambda v: v.get("createdAt", ""))


def folder_name(map_json: dict) -> str:
    """``<key> (<songName> - <levelAuthorName>)`` with illegal chars stripped."""
    md = map_json.get("metadata", {}) or {}
    raw = (
        f"{map_json.get('id', '')} "
        f"({md.get('songName', '')} - {md.get('levelAuthorName', '')})"
    )
    return _ILLEGAL.sub("", raw).strip() or (map_json.get("id") or "song")


# ── Download + extract ──────────────────────────────────────────────────────

def _download_bytes(url: str) -> bytes:
    last_err: Exception | None = None
    for _ in range(_MAX_RETRIES):
        try:
            with _open(url, _ZIP_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 429:
                last_err = BeatSaverError("rate limited: gave up on download")
                _ratelimit_wait(e.headers)
                continue
            last_err = e
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
        time.sleep(1)
    raise BeatSaverError(f"download failed: {last_err}")


def _extract_zip(data: bytes, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    dest_root = dest.resolve()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in archive.namelist():
                target = (dest / member).resolve()
                # Zip-slip guard: never write outside the destination folder.
                if dest_root != target and dest_root not in target.parents:
                    raise BeatSaverError(f"unsafe zip entry: {member}")
            archive.extractall(dest)
    except zipfile.BadZipFile:
        raise BeatSaverError("downloaded file is not a valid zip")


def _already_installed(folder: Path) -> bool:
    return folder.is_dir() and any(
        (folder / n).exists() for n in ("Info.dat", "info.dat")
    )


def download_map(map_json: dict, custom_levels: Path,
                 want_hash: str | None = None) -> Path:
    """Download and extract a resolved map into ``custom_levels``.

    Returns the song folder. If the folder already contains an ``Info.dat`` the
    download is skipped and the existing folder is returned.
    """
    version = _pick_version(map_json, want_hash)
    url = version.get("downloadURL")
    if not url:
        raise BeatSaverError("map version has no downloadURL")

    dest = Path(custom_levels) / folder_name(map_json)
    if _already_installed(dest):
        return dest

    _extract_zip(_download_bytes(url), dest)
    return dest


# ── Convenience entry points ────────────────────────────────────────────────

def install_song(key: str, custom_levels: Path) -> Path:
    """Install a single map by its BeatSaver key/id."""
    return download_map(fetch_map(key, by="key"), custom_levels)


def install_by_hash(song_hash: str, custom_levels: Path) -> Path:
    """Install a single map by its version hash."""
    return download_map(
        fetch_map(song_hash, by="hash"), custom_levels, want_hash=song_hash
    )
