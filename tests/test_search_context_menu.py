"""browser_pagination — the shared tag-insertion context menu.

The main search bar and the Add Random dialog both bind this menu, so these
cover both. Exercised against widget stand-ins so the suite stays UI-free — no
window is ever opened.
"""

from __future__ import annotations

import tkinter as tk

import pytest

from libraries.browser_pagination import (
    TAG_SPECS,
    _has_invalid_tags,
    _parse_tags,
    append_tag,
    paste_into_entry,
)


def _spec(name: str):
    return next(s for s in TAG_SPECS if s.name == name)


class _FakeEntry:
    """Stand-in for a tk.Entry, tracking selection, cursor and focus."""

    def __init__(self, text: str = "", sel: tuple[int, int] | None = None):
        self.text = text
        self.sel = sel
        self.cursor = len(text)
        self.focused = False

    def selection_present(self) -> bool:
        return self.sel is not None

    def delete(self, first: str, last: str) -> None:
        assert (first, last) == ("sel.first", "sel.last")
        assert self.sel is not None
        a, b = self.sel
        self.text = self.text[:a] + self.text[b:]
        self.cursor = a
        self.sel = None

    def insert(self, index: str, value: str) -> None:
        assert index == "insert"
        self.text = self.text[: self.cursor] + value + self.text[self.cursor :]
        self.cursor += len(value)

    def focus_set(self) -> None:
        self.focused = True


# ── Appending tags ───────────────────────────────────────────────────────────

class TestAppendTag:
    def test_empty_box_gets_no_leading_space(self):
        assert append_tag("", _spec("mapper")) == "{mapper}:"

    def test_separates_from_existing_query(self):
        assert append_tag("freedom dive", _spec("bpm")) == "freedom dive {bpm}:"

    def test_does_not_double_the_separator(self):
        assert append_tag("freedom dive ", _spec("bpm")) == "freedom dive {bpm}:"

    def test_yes_no_tags_arrive_prefilled(self):
        assert append_tag("", _spec("favorite")) == "{favorite}:y"

    def test_tags_stack(self):
        q = ""
        for name in ("chroma", "bpm"):
            q = append_tag(q, _spec(name))
        assert q == "{chroma}:y {bpm}:"

    @pytest.mark.parametrize("spec", TAG_SPECS, ids=lambda s: s.name)
    def test_every_listed_tag_produces_a_usable_token(self, spec):
        """The menu offers exactly TAG_SPECS, so each must parse back out."""
        tags, plain = _parse_tags(append_tag("", spec))
        if spec.yes_no:
            assert tags == [(spec.name, "y")]
            assert not _has_invalid_tags(tags)
            assert plain == ""
        else:
            assert tags == []
            assert plain == f"{{{spec.name}}}:"

    def test_appending_after_a_complete_tag_keeps_both(self):
        q = append_tag("{mapper}:psi", _spec("unplayed"))
        tags, plain = _parse_tags(q)
        assert tags == [("mapper", "psi"), ("unplayed", "y")]
        assert not _has_invalid_tags(tags)
        assert plain == ""


# ── Pasting ──────────────────────────────────────────────────────────────────

class TestPasteIntoEntry:
    def test_inserts_at_the_cursor(self):
        e = _FakeEntry("abc")
        e.cursor = 1
        paste_into_entry(e, "XY")
        assert e.text == "aXYbc"

    def test_replaces_the_selection(self):
        e = _FakeEntry("hello world", sel=(0, 5))
        paste_into_entry(e, "bye")
        assert e.text == "bye world"

    def test_empty_clipboard_is_a_no_op(self):
        e = _FakeEntry("abc")
        paste_into_entry(e, "")
        assert e.text == "abc"
        assert not e.focused

    def test_focus_moves_to_the_entry(self):
        e = _FakeEntry("abc")
        paste_into_entry(e, "Z")
        assert e.focused

    def test_widget_errors_are_swallowed(self):
        """A destroyed entry must not surface a TclError from a menu click."""

        class _Dead(_FakeEntry):
            def selection_present(self):
                raise tk.TclError("invalid command name")

        e = _Dead("abc")
        paste_into_entry(e, "Z")
        assert e.text == "abc"
