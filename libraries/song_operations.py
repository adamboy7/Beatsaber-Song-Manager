import json
import shutil
import tkinter as tk
import tkinter.filedialog as fd
from pathlib import Path
from typing import NamedTuple
from libraries import dialogs

from libraries.song_data import SongInfo, compute_song_hash
from libraries.asset_editor import bak_files, restore_files, replace_art
from libraries.audio_utils import find_ffmpeg, _local_dir
from libraries.fs_utils import atomic_write_text
from libraries.player_data import song_level_ids, load_player_stats
from libraries.favorites import backup_player_data, confirm_player_data_write, _atomic_write_player_data


def restore_song_files(
    song: SongInfo, media_player=None, parent: tk.Misc | None = None,
    on_restored=None,
) -> tuple[int, list[str]]:
    """Restore backup files in the song folder. Returns (bak_count, errors).

    A restore rewrites files in place, so it hits the same open-handle problem
    as ``replace_song_audio``: if a ``.bak`` overwrites the audio file the
    media player has loaded, the move fails on Windows while libmpv (and the
    visualizer's ffmpeg) hold it. Playback is stopped and handles released
    first — but only when the audio is actually one of the files being
    restored, so an art- or metadata-only restore doesn't interrupt anything.

    The song is re-parsed here, before ``on_restored`` fires, because a
    restored ``Info.dat`` can point at a *different* audio filename than the
    one currently playing; the caller's resume has to see the updated
    ``audio_path``.

    ``on_restored(reload_playback, was_paused)`` reports whether the song that
    was playing needs restarting — either its audio file was overwritten or
    the restored Info.dat re-pointed it. Fires even when some restores failed,
    so a stopped player never gets stranded.
    """
    baks = bak_files(song)
    if not baks:
        return 0, []

    old_audio = song.audio_path
    overwrites_audio = old_audio is not None and any(
        bak.with_suffix("") == old_audio for bak in baks
    )
    is_loaded = media_player is not None and song is media_player.playing_song
    was_active = is_loaded and media_player.is_active
    was_paused = was_active and media_player.is_paused

    if overwrites_audio and is_loaded:
        media_player.stop_and_wait()
        release = getattr(parent, "_release_song_audio", None) if parent else None
        if release is not None:
            try:
                release(song)
            except Exception:
                pass  # best-effort; the restore may still succeed

    errors = restore_files(song)
    # Re-read Info.dat: display name, cover and audio references may all have
    # been rolled back along with it.
    song._parse()
    recomputed = compute_song_hash(song.folder)
    if recomputed:
        song.song_hash = recomputed

    if on_restored is not None:
        audio_changed = overwrites_audio or song.audio_path != old_audio
        on_restored(was_active and audio_changed, was_paused)
    return len(baks), errors


class ArtReplaceResult(NamedTuple):
    """Outcome of a cover-art replacement.

    ``was_active``/``was_paused`` are set only when playback had to be stopped
    to free a lock on the cover file, so the caller can put it back. They can
    be True even when ``replaced`` is False — the retry may still have failed,
    and playback shouldn't be left stopped either way.
    """

    replaced: bool
    was_active: bool = False
    was_paused: bool = False


def replace_song_art(
    parent: tk.Misc, song: SongInfo, media_player=None,
) -> ArtReplaceResult:
    """Open file dialog and replace cover art.

    Unlike audio, the art swap is attempted *without* interrupting playback:
    normally nothing holds the cover file. If the swap is denied because
    something does (libmpv adopts a song folder's ``cover.jpg`` as external
    cover art for the track it's playing, which locks it on Windows), playback
    is stopped to release it and the swap retried once — so the common case
    stays seamless and the locked case still succeeds.

    Returns synchronously rather than via a callback: there's no deferred
    ffmpeg-download path here the way ``replace_song_audio`` has, and a return
    value lets the caller invalidate its caches *before* resuming playback.
    """
    if not song.cover_path:
        dialogs.show_warning("Replace Art", "This song has no cover image to replace.")
        return ArtReplaceResult(False)
    new_path_str = fd.askopenfilename(
        title="Select New Cover Image",
        filetypes=[("Image files", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"), ("All files", "*.*")],
    )
    if not new_path_str:
        return ArtReplaceResult(False)

    freed = {"was_active": False, "was_paused": False}

    def _free_handles() -> None:
        is_loaded = media_player is not None and song is media_player.playing_song
        freed["was_active"] = is_loaded and media_player.is_active
        freed["was_paused"] = freed["was_active"] and media_player.is_paused
        if is_loaded:
            media_player.stop_and_wait()
        release = getattr(parent, "_release_song_audio", None)
        if release is not None:
            try:
                release(song)
            except Exception:
                pass  # best-effort; the retry may still succeed

    try:
        replace_art(song.cover_path, new_path_str, on_locked=_free_handles)
    except Exception as exc:
        dialogs.show_error("Replace Art Failed", str(exc))
        return ArtReplaceResult(False, freed["was_active"], freed["was_paused"])
    return ArtReplaceResult(True, freed["was_active"], freed["was_paused"])


def prompt_ffmpeg_download(parent: tk.Misc, on_ready=None, on_unavailable=None) -> None:
    """Offer to auto-download a prebuilt ffmpeg (at most once per run).

    Fetches a static BtbN build and drops ffmpeg/ffprobe/ffplay next to the app
    — no manual download or PATH edit needed. Runs in a background thread,
    reporting to the app's status bar; ``find_ffmpeg`` re-probes on every miss,
    so the fresh binaries are picked up live. Progress marshaling and status
    reuse the app's own dispatcher/status bar when ``parent`` exposes them,
    falling back to ``parent.after`` for a plain tk widget.
    """
    from libraries import ffmpeg_installer
    from libraries import app_config

    dispatcher = getattr(parent, "_dispatcher", None)
    dispatch_fn = getattr(dispatcher, "dispatch", None) or (lambda fn: parent.after(0, fn))
    status_bar = getattr(parent, "status_bar", None)
    status_cb = (lambda text: status_bar.config(text=text)) if status_bar is not None else None

    # Download into the per-user app-data folder (writable even for a frozen/
    # read-only install); a side-by-side ffmpeg is still preferred by find_ffmpeg.
    ffmpeg_installer.offer_download_once(
        app_config.app_data_dir(),
        dispatch_fn,
        status_cb=status_cb,
        on_unavailable=on_unavailable,
        on_ready=on_ready,
    )


def replace_song_audio(
    parent: tk.Misc, song: SongInfo, media_player=None, on_replaced=None,
) -> bool:
    """Pick a replacement audio file and open the alignment editor for it.

    Returns True only when the picked file was written *without* the editor —
    which now never happens on the interactive path, since the editor always
    opens. The return value is kept because the deferred ffmpeg path has always
    returned False for "not yet, ask me later", and callers read it that way.

    Why an editor rather than the overwrite this used to do: the common reason
    to replace a song's audio is to swap in a different recording of the same
    music, and two recordings of the same music rarely start at the same
    instant. Writing one over the other unaligned leaves every note in the
    chart pointing at the wrong moment. See ``audio_replace_window`` for the
    timeline model and the padding rules.

    ffmpeg is now required in every case, not just for non-OGG input: the
    editor's waveforms come from it, and so does the aligned render. The
    deferred download path is unchanged otherwise — ``on_ready`` reopens this
    flow once the binaries land.

    ``on_replaced(was_active, was_paused)`` fires after a successful write and
    reports whether playback was interrupted to do it — ``was_active`` is True
    only when this song was the loaded, non-stopped one, letting the caller
    resume it (and re-pause if ``was_paused``). Editing any other song leaves
    playback alone, so both flags come back False.
    """
    if not song.audio_path:
        dialogs.show_warning("Replace Audio", "This song has no audio file to replace.")
        return False
    new_path_str = fd.askopenfilename(
        title="Select New Audio File",
        filetypes=[
            ("Audio files", "*.mp3 *.wav *.ogg *.egg *.m4a *.flac *.opus"),
            ("MP3",         "*.mp3"),
            ("WAV",         "*.wav"),
            ("OGG / EGG",   "*.ogg *.egg"),
            ("M4A",         "*.m4a"),
            ("FLAC",        "*.flac"),
            ("All files",   "*.*"),
        ],
    )
    if not new_path_str:
        return False

    def _open_editor() -> None:
        from libraries.audio_replace_window import open_audio_replace_editor
        open_audio_replace_editor(
            parent, song, Path(new_path_str), media_player, on_replaced,
        )

    if not find_ffmpeg():
        prompt_ffmpeg_download(parent, on_ready=_open_editor)
        return False

    _open_editor()
    return False


def save_song_info(song: SongInfo, song_name: str, author: str, mapper: str) -> str | None:
    """Write song name, artist, and mapper back to Info.dat. Returns error string or None."""
    info_file = None
    for name in ("Info.dat", "info.dat", "INFO.DAT"):
        candidate = song.folder / name
        if candidate.exists():
            info_file = candidate
            break
    if info_file is None:
        return "Info.dat not found in song folder."
    try:
        data = json.loads(info_file.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        return f"Failed to read Info.dat: {exc}"
    try:
        is_v4 = str(data.get("version", "")).startswith("4")
        if is_v4:
            if "song" not in data:
                data["song"] = {}
            data["song"]["title"] = song_name
            data["song"]["author"] = author
            new_mappers = [m.strip() for m in mapper.split(",") if m.strip()] if mapper else []
            for bm in data.get("difficultyBeatmaps", []):
                authors = bm.setdefault("beatmapAuthors", {})
                authors["mappers"] = new_mappers[:]
        else:
            data["_songName"] = song_name
            data["_songAuthorName"] = author
            data["_levelAuthorName"] = mapper
        bak = info_file.parent / (info_file.name + ".bak")
        if not bak.exists():
            shutil.copy2(info_file, bak)
        content = json.dumps(data, ensure_ascii=False, indent=2)
        atomic_write_text(info_file, content)
    except Exception as exc:
        return f"Failed to write Info.dat: {exc}"

    song.song_name = song_name
    song.author = author
    song.mapper = mapper
    if song_name:
        song.display_name = song_name
        if song.sub_name:
            song.display_name += f" {song.sub_name}"
    else:
        song.display_name = song.folder.name
    song.update_search_blob()
    recomputed = compute_song_hash(song.folder)
    if recomputed:
        song.song_hash = recomputed
    return None


def clear_song_score(player_dat_path: Path, song: SongInfo) -> tuple[int, dict] | None:
    """Delete all score entries for song. Returns (removed_count, new_stats) or None on failure."""
    ids_to_clear = set(song_level_ids(song))
    if not confirm_player_data_write():
        return None
    try:
        mtime_before = player_dat_path.stat().st_mtime_ns
        raw = player_dat_path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
        players = data.get("localPlayers", [])
        if not players:
            return None
        entries = players[0].get("levelsStatsData", [])
        before = len(entries)
        players[0]["levelsStatsData"] = [
            e for e in entries if e.get("levelId", "") not in ids_to_clear
        ]
        removed = before - len(players[0]["levelsStatsData"])
        content = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if not _atomic_write_player_data(player_dat_path, content, mtime_before):
            return None
        # Only back up after a successful write, so an aborted operation
        # doesn't leave stray `.dat.bak` files behind.
        backup_player_data(player_dat_path, raw)
        return removed, load_player_stats(player_dat_path)
    except Exception as exc:
        dialogs.show_error("Clear Score Failed", str(exc))
        return None
