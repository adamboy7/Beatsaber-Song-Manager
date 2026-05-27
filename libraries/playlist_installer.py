"""Install a Beat Saber playlist (and all of its missing songs) by handing
the whole .bplist to Mod Assistant via the ``bsplaylist://`` protocol.

Mod Assistant's bsplaylist handler fetches the playlist URL through .NET's
``HttpClient`` (see ``OneClickInstaller.cs`` / ``Playlists.DownloadAll`` in
the upstream repo). HttpClient does not support ``file://``, so we spin up
a tiny loopback HTTP server on a random port that serves the local playlist
file just long enough for Mod Assistant to pick it up.
"""

from __future__ import annotations

import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import quote


class PlaylistInstaller:
    """One-shot bsplaylist:// installer.

    The host wires ``after_fn`` to ``tk.Tk.after`` so all callbacks run on
    the UI thread. ``status_cb`` receives short progress strings, and
    ``complete_cb(success: bool)`` is invoked once when the install
    finishes, times out, or is cancelled.
    """

    # Outer time limit for installs, in seconds. Mod Assistant can take a
    # while when a playlist references dozens of large songs.
    DEFAULT_TIMEOUT = 600

    # If no folders have appeared after this many seconds, assume the user
    # cancelled the Mod Assistant dialog and bail out.
    NO_PROGRESS_GIVEUP = 45

    def __init__(self, custom_levels: Path, after_fn, status_cb, complete_cb):
        self.custom_levels = custom_levels
        self._after = after_fn
        self._status_cb = status_cb
        self._complete_cb = complete_cb
        self._gen = 0
        self._server: HTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._keys: list[str] = []

    # ── Public API ────────────────────────────────────────────────────────────

    @staticmethod
    def has_handler() -> bool:
        """True when ``bsplaylist://`` is registered (i.e. Mod Assistant
        is installed and one-click playlist installs are enabled)."""
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "bsplaylist")
            winreg.CloseKey(key)
            return True
        except (FileNotFoundError, OSError):
            return False

    def cancel(self) -> None:
        self._gen += 1
        self._stop_server()

    def install(
        self,
        playlist_path: Path,
        expected_keys: list[str],
        timeout: int | None = None,
    ) -> bool:
        """Hand ``playlist_path`` to Mod Assistant.

        ``expected_keys`` is the list of BeatSaver keys (or ids) for the
        songs the playlist will need to download. The watcher uses them to
        report progress and to decide when the install has settled.

        Returns ``True`` if the protocol was launched, ``False`` if the
        prerequisites weren't met. Completion is reported asynchronously
        through ``complete_cb``.
        """
        # Tear down any prior install (server + watcher) so back-to-back
        # calls don't leak the previous loopback HTTPServer / thread.
        self.cancel()

        if not self.has_handler():
            self._status_cb(
                "bsplaylist:// handler not found — install Mod Assistant or "
                "enable playlist one-click installs."
            )
            return False

        playlist_path = Path(playlist_path)
        if not playlist_path.is_file():
            self._status_cb(f"Playlist not found: {playlist_path}")
            return False

        try:
            port = self._start_server(playlist_path)
        except Exception as e:
            self._status_cb(f"Could not start local playlist server: {e}")
            return False

        self._keys = [k.lower() for k in expected_keys if k]

        # bsplaylist://playlist/<http url> — matches the format Mod
        # Assistant's OneClickInstaller expects (PR #492 in upstream).
        served_url = f"http://127.0.0.1:{port}/{quote(playlist_path.name)}"
        webbrowser.open(f"bsplaylist://playlist/{served_url}")

        self._gen += 1
        gen = self._gen
        effective_timeout = timeout or self.DEFAULT_TIMEOUT
        self._pulse(gen, 0, effective_timeout)
        self._spawn_watcher(gen, effective_timeout)
        return True

    # ── Loopback HTTP server ──────────────────────────────────────────────────

    def _start_server(self, playlist_path: Path) -> int:
        playlist_path = playlist_path.resolve()
        served_name = playlist_path.name
        served_bytes = playlist_path.read_bytes()
        served_path = "/" + quote(served_name)

        class _Handler(BaseHTTPRequestHandler):
            # Serve exactly one file. Anything else 404s — this keeps the
            # loopback server from being usable as a generic file proxy.
            def do_GET(self):  # noqa: N802 - http.server convention
                if self.path != served_path:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="{served_name}"',
                )
                self.send_header("Content-Length", str(len(served_bytes)))
                self.end_headers()
                self.wfile.write(served_bytes)

            def log_message(self, *_args):  # silence default stderr logging
                return

        # Bind to a free loopback port (port 0 → kernel-assigned).
        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        port = self._server.server_address[1]
        self._server_thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._server_thread.start()
        return port

    def _stop_server(self) -> None:
        if self._server is None:
            return
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass
        self._server = None
        self._server_thread = None

    # ── Progress tracking ─────────────────────────────────────────────────────

    def _installed_count(self) -> int:
        """How many of the expected song keys are present in custom_levels.

        Mod Assistant names song folders ``<key> (<title> - <author>)``,
        so a prefix match against the BeatSaver key is a cheap and reliable
        completion signal.
        """
        if not self._keys:
            return 0
        try:
            names = [
                e.name.lower()
                for e in self.custom_levels.iterdir()
                if e.is_dir()
            ]
        except Exception:
            return 0
        count = 0
        for key in self._keys:
            prefix = key + " "
            if any(name == key or name.startswith(prefix) for name in names):
                count += 1
        return count

    def _pulse(self, gen: int, elapsed: int, timeout: int | None = None) -> None:
        if gen != self._gen:
            return
        # Share the effective timeout with the watcher so the two don't disagree
        # if the caller passed a non-default `timeout` to install().
        effective_timeout = timeout if timeout is not None else self.DEFAULT_TIMEOUT
        dots = "." * (elapsed % 4)
        total = len(self._keys)
        if total:
            self._status_cb(
                f"Installing playlist via Mod Assistant — "
                f"{self._installed_count()}/{total} songs{dots}  ({elapsed}s)"
            )
        else:
            self._status_cb(
                f"Installing playlist via Mod Assistant{dots}  ({elapsed}s)"
            )
        if elapsed < effective_timeout:
            self._after(1000, lambda: self._pulse(gen, elapsed + 1, effective_timeout))

    def _spawn_watcher(self, gen: int, timeout: int) -> None:
        total = len(self._keys)

        def worker():
            deadline = time.monotonic() + max(timeout, 30)
            last_count = self._installed_count()
            stable_since = time.monotonic()

            while time.monotonic() < deadline:
                if gen != self._gen:
                    return
                current = self._installed_count()
                if total and current >= total:
                    # All expected songs landed — give Mod Assistant a
                    # moment to finalize the last folder, then complete.
                    time.sleep(2)
                    if gen == self._gen:
                        self._after(0, lambda: self._on_complete(gen, True))
                    return

                if current != last_count:
                    last_count = current
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since > self.NO_PROGRESS_GIVEUP:
                    # Progress has stalled for too long — bail rather than
                    # waiting out the full DEFAULT_TIMEOUT. Report success
                    # iff at least one expected song made it in before the
                    # stall (matches the post-timeout heuristic below).
                    if gen == self._gen:
                        self._after(
                            0,
                            lambda: self._on_complete(gen, last_count > 0),
                        )
                    return

                time.sleep(2)

            # Hit timeout — surface whatever we got.
            if gen == self._gen:
                self._after(
                    0, lambda: self._on_complete(gen, last_count > 0)
                )

        threading.Thread(target=worker, daemon=True).start()

    def _on_complete(self, gen: int, success: bool) -> None:
        if gen != self._gen:
            return
        self._gen += 1
        self._stop_server()
        self._complete_cb(success)
