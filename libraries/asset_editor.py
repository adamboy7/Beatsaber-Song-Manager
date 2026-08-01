import os
import shutil
import subprocess
import tempfile
from pathlib import Path


from libraries.song_data import SongInfo

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore[assignment]


def bak_files(song: SongInfo) -> list[Path]:
    """Return all .bak files in the song folder."""
    return list(song.folder.glob("*.bak"))


def restore_files(song: SongInfo) -> list[str]:
    """Move every .bak file back to its original name. Returns error strings for any failures."""
    errors = []
    for bak in bak_files(song):
        dest = bak.with_suffix("")
        try:
            shutil.move(str(bak), str(dest))
        except Exception as exc:
            errors.append(f"{bak.name}: {exc}")
    return errors


def replace_art(cover_path: Path, new_path: str, on_locked=None) -> None:
    """Backup the current cover image and replace it with new_path, resized to match the original.

    ``on_locked`` is a last-resort hook for the case where the swap fails
    because something else holds the cover file open — on Windows that denies
    the replace outright (WinError 5). libmpv is the usual culprit: it adopts a
    song folder's ``cover.jpg`` as an external cover-art track for the audio it
    is playing. The callback should free those handles (stop playback); the
    swap is then retried once. Without it the error propagates as before.
    """
    with Image.open(cover_path) as orig:
        orig_size = orig.size
        orig_format = orig.format or cover_path.suffix.lstrip(".").upper()
        if orig_format == "JPG":
            orig_format = "JPEG"

    fd, tmp_str = tempfile.mkstemp(dir=str(cover_path.parent), suffix=".tmp")
    tmp = Path(tmp_str)
    try:
        os.close(fd)
        with Image.open(new_path) as new_img:
            if orig_format == "JPEG":
                new_img = new_img.convert("RGB")
            new_img = new_img.resize(orig_size, Image.LANCZOS)
            new_img.save(tmp_str, format=orig_format)
        bak = cover_path.parent / (cover_path.name + ".bak")
        if not bak.exists():
            shutil.copy2(cover_path, bak)
        try:
            os.replace(tmp_str, cover_path)
        except PermissionError:
            if on_locked is None:
                raise
            on_locked()
            os.replace(tmp_str, cover_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


VORBIS_QUALITY = "5"


def build_audio_filter(offset_ms: int = 0,
                       target_duration_s: float | None = None) -> str:
    """The libavfilter chain that places a replacement on the song's timeline.

    The offset model is ``song_time = new_audio_time + offset_ms / 1000`` — the
    replacement's own t=0 lands ``offset_ms`` into the song. That is the plain
    reading of "this cover starts a bit later", and deliberately *not* Cinema's
    inverted "starts the video earlier" convention: nothing external dictates
    the sign here, so it matches the direction the strip is dragged.

    So a positive offset prepends silence (``adelay``) and a negative one trims
    the head (``atrim``). ``atrim`` alone leaves the surviving frames carrying
    their original timestamps, which would re-insert exactly the lead-in that
    was just cut; ``asetpts=N/SR/TB`` rebuilds them from the sample count so
    the trimmed audio actually starts at zero.

    ``target_duration_s`` is the length the result must come out at — the
    original audio's, when the user asks to keep it. ``apad`` covers a
    replacement that runs short (it pads indefinitely, which is why it is only
    ever emitted alongside the ``-t`` in :func:`audio_render_command` that
    bounds it) and the same ``-t`` cuts one that runs long. Padding is what
    makes a shorter cover safe: the map's notes stay where they are and the
    track simply goes quiet early, rather than the file ending mid-chart.

    Returns ``""`` when there is nothing to do, which is the caller's cue that
    a straight copy or a plain transcode will serve.
    """
    parts: list[str] = []
    offset_ms = int(offset_ms)
    if offset_ms > 0:
        parts.append(f"adelay={offset_ms}:all=1")
    elif offset_ms < 0:
        parts.append(f"atrim=start={-offset_ms / 1000.0:.6f}")
        parts.append("asetpts=N/SR/TB")
    if target_duration_s and target_duration_s > 0:
        parts.append("apad")
    return ",".join(parts)


def audio_render_command(ffmpeg_path: str, src: str | Path, dest: str | Path, *,
                         offset_ms: int = 0,
                         target_duration_s: float | None = None) -> list[str]:
    """Full ffmpeg argv to render ``src`` into an OGG at ``dest``.

    Split out from :func:`replace_audio` so the argv can be asserted on in
    tests without an ffmpeg binary or a real song folder — the filter graph and
    the ``-t`` that bounds ``apad`` are the parts most likely to be got subtly
    wrong.
    """
    cmd = [str(ffmpeg_path), "-y", "-nostdin", "-i", str(src)]
    graph = build_audio_filter(offset_ms, target_duration_s)
    if graph:
        cmd += ["-af", graph]
    if target_duration_s and target_duration_s > 0:
        # An output option, so it bounds the padded stream rather than the
        # input. This is what stops `apad` from running forever.
        cmd += ["-t", f"{target_duration_s:.6f}"]
    cmd += ["-vn", "-c:a", "libvorbis", "-q:a", VORBIS_QUALITY, "-f", "ogg", str(dest)]
    return cmd


def replace_audio(audio_path: Path, new_path: str, ffmpeg_path: str, *,
                  offset_ms: int = 0,
                  target_duration_s: float | None = None) -> None:
    """Backup the current audio file and replace it with ``new_path``.

    Converts to OGG unless the source already is one *and* needs no
    repositioning: an offset or a length to match both require a re-encode, so
    the copy fast path is conditional on there being no work to do. Called with
    neither keyword this behaves exactly as it did before the offset editor
    existed, which is what keeps the plain "just swap the file" path — and its
    ability to run without ffmpeg for an ``.ogg`` — intact.
    """
    new = Path(new_path)
    ext = new.suffix.lower()
    needs_render = bool(build_audio_filter(offset_ms, target_duration_s))

    fd, tmp_str = tempfile.mkstemp(dir=str(audio_path.parent), suffix=".tmp")
    tmp = Path(tmp_str)
    try:
        os.close(fd)
        if ext in (".egg", ".ogg") and not needs_render:
            shutil.copy2(new, tmp_str)
        else:
            if not ffmpeg_path:
                raise RuntimeError(
                    "ffmpeg not available — cannot convert audio.\n"
                    "Place ffmpeg next to this app or add it to PATH."
                )
            # Invoke ffmpeg directly with CREATE_NO_WINDOW rather than going
            # through pydub. Avoids the global `subprocess.Popen` monkey-patch
            # that would otherwise be needed to suppress the conversion's
            # console window.
            creation_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            cmd = audio_render_command(
                ffmpeg_path, new_path, tmp_str,
                offset_ms=offset_ms, target_duration_s=target_duration_s,
            )
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=creation_flag,
            )
            if result.returncode != 0:
                err = (result.stderr or b"").decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"ffmpeg conversion failed (exit {result.returncode}):\n{err.strip()}"
                )
            if not os.path.getsize(tmp_str):
                raise RuntimeError(
                    "The offset moved the replacement entirely outside the "
                    "song, so there would be no audio left. Move it back "
                    "towards the start."
                )
        bak = audio_path.parent / (audio_path.name + ".bak")
        if not bak.exists():
            shutil.copy2(audio_path, bak)
        os.replace(tmp_str, audio_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
