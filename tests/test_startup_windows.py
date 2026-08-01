"""Opening the queue/visualizer windows at launch.

Three properties are worth holding onto here.

The flags are *one-shot*: ``_on_loaded`` runs again on every F5 and after every
install, and a window that reopened each time would be worse than useless.

Headless never reaches this code at all — every CLI batch path exits before a
``SongBrowser`` exists, which is what stops ``--randomAdd`` on a machine with
these keys set from trying to open a Tk window.

Opening is *deferred until the main window is mapped*. Creating a Toplevel
before Windows has finished activating its parent leaves it stacked above the
browser until clicked, which is the bug this deferral fixes; the browser is
lifted afterwards so the launch ends with the song list in front.

Nothing here constructs a real window: ``FakeBrowser`` inherits the mixin and
stubs the two openers plus the few Tk calls they reach, so the sequencing and
the flag bookkeeping are what get exercised.
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


class FakeBrowser(BrowserPlaylistsMixin):
    """Just enough SongBrowser for ``_open_startup_windows``.

    Inherits the mixin so the real ``_open_startup_windows`` and its
    ``_ready_for_startup_windows`` helper run against each other exactly as
    they do in production; only the two window openers and the handful of Tk
    calls they reach are stubbed out.

    ``after`` runs the callback immediately rather than scheduling it, so a
    deferral loop resolves synchronously inside ``run()``; ``mapped_after``
    controls how many checks pass before the window reports itself on screen.
    """

    def __init__(self, queue=False, visualizer=False, fail: str | None = None,
                 mapped_after: int = 0):
        self._startup_open_queue = queue
        self._startup_open_visualizer = visualizer
        self._fail = fail
        self._mapped_after = mapped_after
        self.opened: list[str] = []
        self.status_bar = _StatusBar()
        self.map_checks = 0
        self.deferrals = 0
        self.raised = 0
        self.focused = 0

    # ── Tk surface ───────────────────────────────────────────────────────────

    def winfo_ismapped(self):
        self.map_checks += 1
        return self.map_checks > self._mapped_after

    def winfo_viewable(self):
        return self.map_checks > self._mapped_after

    def after(self, _ms, callback):
        self.deferrals += 1
        callback()

    def lift(self):
        self.raised += 1

    def focus_force(self):
        self.focused += 1

    # ── The windows ──────────────────────────────────────────────────────────

    def _open(self, name):
        if self._fail == name:
            raise RuntimeError(f"{name} exploded")
        self.opened.append(name)

    def _open_queue_window(self):
        self._open("queue")

    def _open_visualizer_window(self):
        self._open("visualizer")

    def run(self):
        self._open_startup_windows()
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

    browser._open_startup_windows()  # a later refresh
    browser._open_startup_windows()
    assert browser.opened == ["queue", "visualizer"]
    assert browser._startup_open_queue is False
    assert browser._startup_open_visualizer is False


def test_a_window_that_fails_to_open_does_not_abort_the_load():
    """``_on_loaded`` is what finishes populating the library list.

    The visualizer needs libmpv and ffmpeg and can legitimately fail on a given
    machine. A preference about window layout must not be able to leave the
    user staring at an empty song list.
    """
    browser = FakeBrowser(queue=True, visualizer=True, fail="queue").run()
    assert browser.opened == ["visualizer"]  # the other one still ran
    assert "Could not open the queue window" in browser.status_bar.text


def test_a_failure_still_clears_the_flag():
    """Otherwise a broken visualizer retries on every single refresh."""
    browser = FakeBrowser(visualizer=True, fail="visualizer").run()
    assert browser._startup_open_visualizer is False


# ── Stacking against an unmapped main window ─────────────────────────────────


def test_opening_waits_until_the_main_window_is_on_screen():
    """The mapping race is why the windows used to come up stuck on top.

    ``_on_loaded`` can fire on the first dispatcher tick after ``mainloop()``
    starts, while Windows is still activating the main window. A child
    Toplevel created in that gap stacks against a parent that isn't in the
    foreground yet and ends up above the browser until clicked.
    """
    browser = FakeBrowser(queue=True, mapped_after=3).run()

    assert browser.deferrals == 3, "should have waited, not opened immediately"
    assert browser.opened == ["queue"]


def test_nothing_is_deferred_once_the_window_is_already_up():
    """The common case — a library slow enough that mainloop settled first."""
    browser = FakeBrowser(queue=True, visualizer=True).run()
    assert browser.deferrals == 0
    assert browser.opened == ["queue", "visualizer"]


def test_the_wait_is_bounded():
    """A window launched minimized never maps.

    Waiting forever would silently drop a preference the user set, so the
    windows open anyway once patience runs out — wrong stacking beats no
    window at all.
    """
    browser = FakeBrowser(queue=True, visualizer=True, mapped_after=10_000).run()
    assert browser.opened == ["queue", "visualizer"]
    assert browser.deferrals < 20, "bounded, not unbounded"


def test_no_deferral_loop_when_neither_flag_is_set():
    """The check runs after the flags, so a default launch costs nothing."""
    browser = FakeBrowser(mapped_after=10_000).run()
    assert browser.deferrals == 0
    assert browser.map_checks == 0


def test_the_browser_ends_up_in_front():
    """Each Toplevel takes the foreground as it is created.

    Without lifting afterwards the launch state is 'whichever window was
    constructed last' — with both keys set, the visualizer, sitting over the
    song list the user actually came to look at.
    """
    browser = FakeBrowser(queue=True, visualizer=True).run()
    assert browser.raised == 1
    assert browser.focused == 1


def test_the_browser_is_not_disturbed_when_nothing_opens():
    """A default launch must not have its focus poked at all."""
    browser = FakeBrowser().run()
    assert (browser.raised, browser.focused) == (0, 0)


def test_the_browser_is_still_raised_after_a_window_fails():
    """The failure path leaves the foreground in the same odd state."""
    browser = FakeBrowser(queue=True, fail="queue").run()
    assert browser.opened == []
    assert (browser.raised, browser.focused) == (1, 1)


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
