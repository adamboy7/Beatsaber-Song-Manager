"""A/B preview for the audio replacement editor.

Plays the song's current audio against a candidate replacement so the user can
hear whether the alignment they dragged is right. Three modes:

``both``
    Split stereo — the original in the left ear, the replacement in the right.
    The same technique Cinema's README recommends for video sync, and it works
    here for the same reason: two copies of the same music a few tens of
    milliseconds apart are trivially audible as a flam when separated by ear,
    and almost impossible to pick out when summed to the centre.
``original`` / ``new``
    One track alone, for judging *which* recording is wanted rather than where
    it sits. Comparing arrangements is what the split can't do — panned to one
    ear with the other ear occupied, neither take gets a fair hearing.

Everything is mixed here, into one buffer this module owns, and handed to a
single libmpv instance as a plain WAV. That is the lesson ``cinema_preview``
paid for: two demuxers fed into one filter graph do not resume from the same
instant after a seek, and sample offsets in a single array cannot disagree.
There is one clock, so there is no drift to correct and no watchdog to run.

The baked WAV is already on the **song's** timeline — a positive offset is
prepended silence, a negative one a trimmed head — so mpv's reported position
*is* the song position, with no mapping in between.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import wave
from pathlib import Path

from libraries.cinema_preview import (
    CHANNELS,
    EAR_BLEED,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    PcmSource,
    PreviewError,
    add_pcm,
    pan_ears,
    prop_get,
    prop_set,
    wav_bytes_for_shift,
)
from libraries.mpv_backend import load_mpv

MODE_BOTH = "both"
MODE_ORIGINAL = "original"
MODE_NEW = "new"
MODES = (MODE_BOTH, MODE_ORIGINAL, MODE_NEW)

_WAV_HEADER_BYTES = 44


def mix_ab(original_pcm: bytes, new_pcm: bytes, offset_ms: int, mode: str, *,
           ear_bleed: float = EAR_BLEED) -> bytes:
    """Build exactly what should be heard, as one stereo PCM buffer.

    ``offset_ms`` positions the replacement the same way the editor and the
    final render do: ``song_time = new_audio_time + offset_ms / 1000``, so a
    positive value prepends silence and a negative one trims the head. The
    original is never shifted — it *is* the reference the axis is drawn
    against.

    ``ear_bleed`` leaves a little of each track in the opposite ear. Fully
    hard-panned tracks stop sounding like one performance heard twice and start
    sounding like two unrelated ones, which makes the flam that reveals a
    misalignment harder to hear rather than easier.
    """
    silence, body = wav_bytes_for_shift(bytes(new_pcm), int(offset_ms))
    shifted_new = silence + body
    if mode == MODE_ORIGINAL:
        return bytes(original_pcm)
    if mode == MODE_NEW:
        return shifted_new
    return add_pcm(
        pan_ears(bytes(original_pcm), 1.0, ear_bleed),  # original hard-ish left
        pan_ears(shifted_new, ear_bleed, 1.0),          # replacement right
    )


def bake_key(offset_ms: int, mode: str) -> tuple[str, int]:
    """What a baked WAV's contents depend on.

    ``original`` alone doesn't depend on the offset at all, so nudging while
    soloed to it must not trigger a rebake — otherwise auditioning the
    reference stutters every time an arrow key is touched, for a file that
    would come out byte-identical.
    """
    return (mode, 0 if mode == MODE_ORIGINAL else int(offset_ms))


class AbPreview:
    """One libmpv instance playing a mix of the original and the replacement."""

    def __init__(self, original_path: Path, new_path: Path, *,
                 volume: int | None = None):
        self._original_path = Path(original_path)
        self._new_path = Path(new_path)
        self._volume = volume

        self._original: PcmSource | None = None
        self._new: PcmSource | None = None

        self._player = None
        self._offset_ms = 0
        self._mode = MODE_BOTH
        self._baked_key: tuple[str, int] | None = None
        self._ended = False

        self._tmpdir: Path | None = None
        self._bake_parity = 0
        self._bake_lock = threading.Lock()
        self._baking = False
        self._bake_gen = 0

    # ── Lifecycle ────────────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._player is not None

    @property
    def ended(self) -> bool:
        return self._ended

    @property
    def duration_s(self) -> float:
        """Length of the longer of the two takes, in song time."""
        a = self._original.duration_s if self._original else 0.0
        b = self._new.duration_s if self._new else 0.0
        return max(a, b)

    def decode(self) -> None:
        """Decode both files to PCM. Blocking — call from a worker thread.

        Separate from :meth:`start` so the editor can decode once and then
        start, stop and restart the preview without paying for it again; on a
        four-minute song the two decodes are most of a second.
        """
        if self._original is None:
            self._original = PcmSource.decode(self._original_path)
        if self._new is None:
            self._new = PcmSource.decode(self._new_path)

    def start(self, at_song_s: float, offset_ms: int, mode: str = MODE_BOTH) -> None:
        """Bake the mix and begin playing from ``at_song_s``.

        Blocking (it writes a WAV of the whole song); safe on a worker thread.
        Raises ``PreviewError`` if libmpv is unavailable or the offset leaves
        nothing to play.
        """
        mpv_mod = load_mpv()
        if mpv_mod is None:
            raise PreviewError("libmpv is not available")
        self.decode()

        self.stop()
        self._offset_ms = int(offset_ms)
        self._mode = mode if mode in MODES else MODE_BOTH
        self._ended = False
        self._tmpdir = Path(tempfile.mkdtemp(prefix="bssm-audio-preview-"))

        wav = self._write_bake(bake_key(self._offset_ms, self._mode))
        if wav.stat().st_size <= _WAV_HEADER_BYTES:
            self._cleanup_tmp()
            raise PreviewError(
                f"an offset of {self._offset_ms:+d} ms moves the replacement "
                "entirely outside the song"
            )

        try:
            player = mpv_mod.MPV(
                idle="yes", keep_open="yes", osd_level=0, vid="no",
                hr_seek="yes",
                input_default_bindings=False, input_vo_keyboard=False,
                input_cursor=False, cursor_autohide="no",
            )
        except Exception as exc:
            self._cleanup_tmp()
            raise PreviewError(f"could not create the player: {exc}")

        if self._volume is not None:
            try:
                player.volume = self._volume
            except Exception:
                pass
        try:
            player.observe_property("eof-reached", self._on_eof)
        except Exception:
            pass

        try:
            player.loadfile(str(wav), start=f"{max(0.0, at_song_s):.3f}")
        except Exception as exc:
            try:
                player.terminate()
            except Exception:
                pass
            self._cleanup_tmp()
            raise PreviewError(f"could not load the preview: {exc}")

        self._player = player
        self._baked_key = bake_key(self._offset_ms, self._mode)

    def stop(self) -> None:
        with self._bake_lock:
            self._bake_gen += 1
        player, self._player = self._player, None
        if player is not None:
            try:
                player.terminate()
            except Exception:
                pass
        self._cleanup_tmp()
        self._baked_key = None

    def poll(self) -> None:
        """Nothing to service; present so callers can treat engines alike."""

    def _on_eof(self, _name, value) -> None:
        if value:
            self._ended = True

    def song_position_s(self) -> float | None:
        """Where playback is, in song time, or None while it is still loading."""
        if self._player is None:
            return None
        pos = prop_get(self._player, "time-pos")
        if pos is None:
            return None
        try:
            return float(pos)
        except (TypeError, ValueError):
            return None

    # ── Offset and mode ──────────────────────────────────────────────────────

    def set_offset_ms(self, offset_ms: int, *, settling: bool = False) -> None:
        """Apply a new offset, rebaking unless a drag is still in progress.

        ``settling`` is what keeps a drag from queueing a bake per motion
        event: the editor passes True while the button is down and calls again
        with False on release. Unlike the Cinema engine there is no
        ``audio-delay`` approximation to ride on in the meantime — both tracks
        live in one baked file, so nothing mpv can be told will move one of
        them relative to the other. The preview simply keeps playing the last
        alignment until the drag ends, which is also the only reading that
        isn't a moving target.
        """
        self._offset_ms = int(offset_ms)
        if self._player is None or settling:
            return
        self._request_bake()

    def set_mode(self, mode: str) -> None:
        if mode not in MODES or mode == self._mode:
            return
        self._mode = mode
        if self._player is not None:
            self._request_bake()

    @property
    def mode(self) -> str:
        return self._mode

    # ── Baking ───────────────────────────────────────────────────────────────

    def _request_bake(self) -> None:
        """Queue a bake for the current state, coalescing with any in flight."""
        with self._bake_lock:
            if bake_key(self._offset_ms, self._mode) == self._baked_key:
                return
            if self._baking:
                return  # the running worker re-reads the target when it loops
            self._baking = True
            gen = self._bake_gen
        threading.Thread(target=self._bake_worker, args=(gen,), daemon=True).start()

    def _bake_worker(self, gen: int) -> None:
        """Bake and swap until the playing file matches the current state.

        Loops rather than baking once because the offset can move again while a
        bake is running; re-reading the target on each pass is what makes a
        held-down arrow key produce one final correct bake instead of a queue
        of stale ones.
        """
        try:
            while True:
                with self._bake_lock:
                    target = bake_key(self._offset_ms, self._mode)
                    if gen != self._bake_gen or target == self._baked_key:
                        self._baking = False
                        return
                try:
                    wav = self._write_bake(target)
                except Exception:
                    with self._bake_lock:
                        self._baking = False
                    return
                if wav.stat().st_size <= _WAV_HEADER_BYTES:
                    with self._bake_lock:
                        self._baking = False
                    return
                if not self._swap_in(wav, gen, target):
                    return
        finally:
            with self._bake_lock:
                self._baking = False

    def _swap_in(self, wav: Path, gen: int, key: tuple[str, int]) -> bool:
        """Reload the player onto ``wav`` at the position it had. False if torn down."""
        player = self._player
        with self._bake_lock:
            if player is None or gen != self._bake_gen:
                return False
        at = self.song_position_s() or 0.0
        was_paused = bool(prop_get(player, "pause"))
        try:
            player.loadfile(str(wav), start=f"{max(0.0, at):.3f}")
        except Exception:
            return False
        if was_paused:
            prop_set(player, "pause", True)
        with self._bake_lock:
            if gen != self._bake_gen:
                return False
            self._baked_key = key
        return True

    def _write_bake(self, key: tuple[str, int]) -> Path:
        """Render ``key``'s mix to a fresh WAV under the temp dir."""
        if self._tmpdir is None:
            self._tmpdir = Path(tempfile.mkdtemp(prefix="bssm-audio-preview-"))
        mode, offset_ms = key
        self._bake_parity ^= 1
        out = self._tmpdir / f"preview-{self._bake_parity}.wav"
        data = mix_ab(
            self._original.pcm if self._original else b"",
            self._new.pcm if self._new else b"",
            offset_ms, mode,
        )
        with wave.open(str(out), "wb") as wav:
            wav.setnchannels(CHANNELS)
            wav.setsampwidth(SAMPLE_WIDTH)
            wav.setframerate(SAMPLE_RATE)
            if data:
                wav.writeframes(data)
        return out

    def _cleanup_tmp(self) -> None:
        tmpdir, self._tmpdir = self._tmpdir, None
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)
