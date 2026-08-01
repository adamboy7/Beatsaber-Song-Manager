"""Opening the queue/visualizer windows at launch.

Two properties are worth holding onto here. The first is that the flags are
*one-shot*: ``_on_loaded`` runs again on every F5 and after every install, and
a window that reopened each time would be worse than useless. The second is
that headless never reaches this code at all — every CLI batch path exits
before a ``SongBrowser`` exists, which is what stops ``--randomAdd`` on a
machine with these keys set from trying to open a Tk window.

The mixin methods are called unbound against a stub, so no window is ever
constructed; what is under test is the sequencing and the flag bookkeeping, not
Tk.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from libraries.browser_playlists import BrowserPlaylistsMixin

ROOT = Path(__file__).resolve().parent.parent


class _StatusBar:
    def __init__(self):
        self.text = ""

    def config(self, text=""):
        self.text = text


class FakeBrowser:
    """Just enough SongBrowser for ``_open_startup_windows``."""

    def __init__(self, queue=False, visualizer=False, fail: str | None = None):
        self._startup_open_queue = queue
        self._startup_open_visualizer = visualizer
        self._fail = fail
        self.opened: list[str] = []
        self.status_bar = _StatusBar()

    def _open(self, name):
        if self._fail == name:
            raise RuntimeError(f"{name} exploded")
        self.opened.append(name)

    def _open_queue_window(self):
        self._open("queue")

    def _open_visualizer_window(self):
        self._open("visualizer")

    def run(self):
        BrowserPlaylistsMixin._open_startup_windows(self)
        return self


def test_nothing_opens_when_both_flags_are_off():
    """The default, and the path every user who never edits config.json takes."""
    assert FakeBrowser().run().opened == []


@pytest.mark.parametrize("kwargs,expected", [
    (dict(queue=True), ["queue"]),
    (dict(visualizer=True), ["visualizer"]),
    (dict(queue=True, visualizer=True), ["queue", "visualizer"]),
])
def test_each_flag_opens_only_its_own_window(kwargs, expected):
    assert FakeBrowser(**kwargs).run().opened == expected


def test_the_flags_are_one_shot():
    """F5 and post-install reloads both re-enter ``_on_loaded``.

    Without the clear, refreshing the library would reopen a window the user
    had just closed — every time, for the whole session.
    """
    browser = FakeBrowser(queue=True, visualizer=True).run()
    assert browser.opened == ["queue", "visualizer"]

    BrowserPlaylistsMixin._open_startup_windows(browser)  # a later refresh
    BrowserPlaylistsMixin._open_startup_windows(browser)
    assert browser.opened == ["queue", "visualizer"]
    assert browser._startup_open_queue is False
    assert browser._startup_open_visualizer is False


def test_a_window_that_fails_to_open_does_not_abort_the_load():
    """``_on_loaded`` is what finishes populating the library list.

    The visualizer needs libmpv and ffmpeg and can legitimately fail on a given
    machine. An undocumented, hand-edited debug key must not be able to leave
    the user staring at an empty song list.
    """
    browser = FakeBrowser(queue=True, visualizer=True, fail="queue").run()
    assert browser.opened == ["visualizer"]  # the other one still ran
    assert "Could not open the queue window" in browser.status_bar.text


def test_a_failure_still_clears_the_flag():
    """Otherwise a broken visualizer retries on every single refresh."""
    browser = FakeBrowser(visualizer=True, fail="visualizer").run()
    assert browser._startup_open_visualizer is False


# ── Structural guarantees ────────────────────────────────────────────────────


def _browser_source() -> str:
    return (ROOT / "Browser.py").read_text(encoding="utf-8")


def test_the_startup_flags_are_sampled_once_at_construction():
    """Read in ``__init__``, not in ``_on_loaded``.

    Sampling once means editing config.json while the app is running cannot
    make a later refresh spawn windows — the flag it would consult was already
    consumed.
    """
    source = _browser_source()
    for getter in ("get_open_queue_on_startup", "get_open_visualizer_on_startup"):
        assert source.count(f"app_config.{getter}()") == 1


def test_headless_never_constructs_a_browser_before_exiting():
    """The structural reason ``--install``/``--randomAdd`` ignore these keys.

    The flags live on ``SongBrowser`` and are consumed in ``_on_loaded``, so
    "headless doesn't touch them" holds exactly as long as headless never
    builds one. That is currently true because every batch branch in ``main``
    ends in ``sys.exit`` — this pins it, since a future refactor that fell
    through into the GUI construction would otherwise fail silently and only on
    a machine with the keys set.
    """
    tree = ast.parse(_browser_source())
    main = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "main")

    constructions = [
        n for n in ast.walk(main)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "SongBrowser"
    ]
    assert len(constructions) == 1, "more than one GUI entry point to audit"

    # The single construction must be at the top level of main's body — i.e.
    # reached only by falling past every headless branch, never from inside one.
    top_level = any(
        isinstance(stmt, ast.Assign)
        and any(isinstance(n, ast.Call)
                and getattr(n.func, "id", None) == "SongBrowser"
                for n in ast.walk(stmt))
        for stmt in main.body
    )
    assert top_level, "SongBrowser is constructed inside a branch of main()"


def test_app_config_is_the_only_reader_of_the_startup_keys():
    """The raw key strings must not be dug out of the config dict elsewhere."""
    for key in ("open_queue_on_startup", "open_visualizer_on_startup"):
        readers = [
            path.relative_to(ROOT).as_posix()
            for path in [*(ROOT / "libraries").glob("*.py"), ROOT / "Browser.py"]
            if f'"{key}"' in path.read_text(encoding="utf-8")
        ]
        assert readers == ["libraries/app_config.py"], readers
