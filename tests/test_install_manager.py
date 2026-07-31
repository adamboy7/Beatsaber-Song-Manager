"""install_manager — the worker's success and failure dispatches.

The failure path is the interesting one. ``_worker`` catches on a background
thread but reports on the UI thread, so the exception has to survive the hop.
Python deletes an ``except ... as`` name at the end of the block, so a lambda
that closes over it plainly raises ``NameError`` when the dispatcher finally
runs it — and ``Dispatcher._pump`` swallows callback exceptions by design, so
the failure would be silent: no dialog, no status update, no song.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from libraries import install_manager as im


class _Recorder:
    """Stands in for the Tk dispatcher and the browser's callbacks."""

    def __init__(self):
        self.pending: list = []
        self.status: list[str] = []
        self.reloads = 0

    def dispatch(self, fn):
        self.pending.append(fn)  # queued, not run — like the real pump

    def status_cb(self, text):
        self.status.append(text)

    def reload_cb(self):
        self.reloads += 1

    def pump(self):
        """Run what was queued, as the UI thread eventually would."""
        queued, self.pending = self.pending, []
        for fn in queued:
            fn()


@pytest.fixture
def rec():
    return _Recorder()


@pytest.fixture
def manager(rec, tmp_path):
    return im.InstallManager(tmp_path, rec.dispatch, rec.status_cb, rec.reload_cb)


class TestWorkerFailure:
    def test_failure_reports_after_the_thread_hop(self, manager, rec, monkeypatch):
        """The regression: the callback must not raise when finally run."""
        def _boom(song_id, dest):
            raise RuntimeError("BeatSaver said no")

        monkeypatch.setattr(im.bs, "install_song", _boom)
        manager._active_ids.add("abc")
        manager._worker("abc")

        rec.pump()  # would raise NameError before the fix

        assert rec.status == ["Could not install abc: BeatSaver said no"]
        assert rec.reloads == 1

    def test_failure_frees_the_id_for_a_retry(self, manager, rec, monkeypatch):
        monkeypatch.setattr(
            im.bs, "install_song",
            lambda song_id, dest: (_ for _ in ()).throw(OSError("disk full")),
        )
        manager._active_ids.add("abc")
        manager._worker("abc")
        rec.pump()
        assert "abc" not in manager._active_ids

    def test_the_id_is_only_freed_on_the_ui_thread(self, manager, monkeypatch):
        """Until the dispatch runs, the in-flight guard must still hold."""
        monkeypatch.setattr(
            im.bs, "install_song",
            lambda song_id, dest: (_ for _ in ()).throw(OSError("nope")),
        )
        manager._active_ids.add("abc")
        manager._worker("abc")
        assert "abc" in manager._active_ids  # dispatched, not yet pumped


class TestWorkerSuccess:
    def test_success_reports_and_reloads(self, manager, rec, monkeypatch):
        monkeypatch.setattr(im.bs, "install_song", lambda song_id, dest: None)
        manager._active_ids.add("abc")
        manager._worker("abc")
        rec.pump()
        assert rec.status == ["Installed abc."]
        assert rec.reloads == 1
        assert "abc" not in manager._active_ids


# ── Codebase-wide guard ──────────────────────────────────────────────────────

def _deferred_except_captures() -> list[str]:
    """Every lambda/def that uses an ``except ... as`` name after the block.

    Names inside default-value expressions don't count: those are evaluated
    when the callable is created, which is still inside the block — that is
    exactly the ``lambda exc=e:`` fix, and it must not be flagged.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    skip = {"Reference", "build", "dist", "__pycache__", ".git", "tests"}
    found: list[str] = []
    for path in sorted(root.glob("**/*.py")):
        if skip & set(path.relative_to(root).parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        handlers = (n for n in ast.walk(tree)
                    if isinstance(n, ast.ExceptHandler) and n.name)
        for h in handlers:
            for inner in ast.walk(h):
                if not isinstance(inner, (ast.Lambda, ast.FunctionDef)):
                    continue
                a = inner.args
                bound = {x.arg for x in a.args + a.kwonlyargs + a.posonlyargs}
                if a.vararg:
                    bound.add(a.vararg.arg)
                if a.kwarg:
                    bound.add(a.kwarg.arg)
                if h.name in bound:
                    continue
                defaults = {id(d) for d in a.defaults
                            + [k for k in a.kw_defaults if k]}
                body = inner.body if isinstance(inner.body, list) else [inner.body]
                if any(isinstance(n, ast.Name) and n.id == h.name
                       and id(n) not in defaults
                       for stmt in body for n in ast.walk(stmt)):
                    rel = path.relative_to(root)
                    found.append(f"{rel}:{inner.lineno} captures 'as {h.name}'")
    return found


def test_no_deferred_except_name_captures():
    """`except ... as e` plus a deferred lambda is the trap that bit here.

    This module dispatches a lot of background work back to the UI thread, so
    guard the shape everywhere rather than just at the one site that broke.
    """
    assert _deferred_except_captures() == []


def test_the_guard_catches_the_original_shape(tmp_path, monkeypatch):
    """The guard is only worth having if it fails on the pre-fix code."""
    bad = tmp_path / "sample.py"
    bad.write_text(
        "def f(dispatch):\n"
        "    try:\n"
        "        g()\n"
        "    except Exception as e:\n"
        "        dispatch(lambda: h(e))\n",
        encoding="utf-8",
    )
    tree = ast.parse(bad.read_text(encoding="utf-8"))
    handler = next(n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler))
    lam = next(n for n in ast.walk(handler) if isinstance(n, ast.Lambda))
    assert not {x.arg for x in lam.args.args}  # nothing bound
    assert any(isinstance(n, ast.Name) and n.id == "e" for n in ast.walk(lam.body))
