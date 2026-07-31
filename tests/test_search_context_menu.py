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
    bind_tag_menu,
    copy_from_entry,
    cut_from_entry,
    insert_tag_into_entry,
    paste_into_entry,
    selected_text,
)


def _spec(name: str):
    return next(s for s in TAG_SPECS if s.name == name)


class _FakeEntry:
    """Stand-in for a tk.Entry, tracking selection, cursor, focus, clipboard."""

    def __init__(self, text: str = "", sel: tuple[int, int] | None = None,
                 clipboard: str = ""):
        self.text = text
        self.sel = sel
        self.cursor = len(text)
        self.focused = False
        self.clipboard = clipboard

    def get(self) -> str:
        return self.text

    def index(self, spec: str) -> int:
        assert self.sel is not None
        return self.sel[0] if spec == "sel.first" else self.sel[1]

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

    def select_clear(self) -> None:
        self.sel = None

    def icursor(self, index) -> None:
        self.cursor = len(self.text) if index == "end" else index

    def clipboard_clear(self) -> None:
        self.clipboard = ""

    def clipboard_append(self, value: str) -> None:
        self.clipboard += value

    # ── Tk's own Entry bindings, transcribed from tk8.6/entry.tcl ────────────
    #
    # Modelled rather than mocked, because the bug was a disagreement between
    # these two: they treat a stale selection differently, so a test that
    # merely asserted "selection cleared" would not show why that matters.

    def type_key(self, s: str) -> None:
        """``tk::EntryInsert`` — replaces the selection only if insert is in it."""
        if self.sel is not None and self.sel[0] <= self.cursor <= self.sel[1]:
            self.delete("sel.first", "sel.last")
        self.insert("insert", s)

    def backspace(self) -> None:
        """``tk::EntryBackspace`` — deletes the selection whenever present."""
        if self.selection_present():
            self.delete("sel.first", "sel.last")
        elif self.cursor > 0:
            self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
            self.cursor -= 1


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


# ── Copying ──────────────────────────────────────────────────────────────────

class TestSelectedText:
    def test_partial_selection(self):
        assert selected_text(_FakeEntry("hello world", sel=(6, 11))) == "world"

    def test_whole_selection(self):
        assert selected_text(_FakeEntry("hello", sel=(0, 5))) == "hello"

    def test_nothing_selected(self):
        assert selected_text(_FakeEntry("hello")) == ""

    def test_widget_errors_read_as_no_selection(self):
        class _Dead(_FakeEntry):
            def selection_present(self):
                raise tk.TclError("invalid command name")

        assert selected_text(_Dead("hello", sel=(0, 5))) == ""


class TestCopyFromEntry:
    def test_copies_the_selection(self):
        e = _FakeEntry("hello world", sel=(6, 11))
        assert copy_from_entry(e) == "world"
        assert e.clipboard == "world"

    def test_copies_a_full_selection(self):
        e = _FakeEntry("{bpm}:<=140", sel=(0, 11))
        copy_from_entry(e)
        assert e.clipboard == "{bpm}:<=140"

    def test_no_selection_leaves_the_clipboard_alone(self):
        """Copying nothing must not clobber what the user already had."""
        e = _FakeEntry("hello", clipboard="something precious")
        assert copy_from_entry(e) == ""
        assert e.clipboard == "something precious"

    def test_replaces_rather_than_appends(self):
        e = _FakeEntry("abc", sel=(0, 3), clipboard="stale")
        copy_from_entry(e)
        assert e.clipboard == "abc"

    def test_widget_errors_are_swallowed(self):
        class _Dead(_FakeEntry):
            def clipboard_clear(self):
                raise tk.TclError("invalid command name")

        e = _Dead("abc", sel=(0, 3))
        assert copy_from_entry(e) == ""

    def test_copy_then_paste_round_trips(self):
        """The two halves of the menu should compose."""
        src = _FakeEntry("{mapper}:psi", sel=(0, 12))
        copied = copy_from_entry(src)
        dest = _FakeEntry("")
        paste_into_entry(dest, copied)
        assert dest.text == "{mapper}:psi"


# ── Inserting a tag while text is selected ───────────────────────────────────

class TestInsertTagIntoEntry:
    """Regression: a tag picked while text was highlighted left a stale
    selection. The cursor moved to the end, outside it, and Tk's two editing
    bindings then disagreed — typing appended instead of replacing, while
    backspace wiped the highlight."""

    def _entry_with_selection(self):
        e = _FakeEntry("freedom dive", sel=(0, 7))
        return e, _Var("freedom dive")

    def test_selection_is_dropped(self):
        e, var = self._entry_with_selection()
        insert_tag_into_entry(e, var, _spec("bpm"))
        assert not e.selection_present()

    def test_tag_is_appended_not_substituted(self):
        e, var = self._entry_with_selection()
        insert_tag_into_entry(e, var, _spec("bpm"))
        assert var.get() == "freedom dive {bpm}:"

    def test_cursor_lands_at_the_end_with_focus(self):
        e, var = self._entry_with_selection()
        e.text = var.get()
        insert_tag_into_entry(e, var, _spec("bpm"))
        assert e.focused
        assert e.cursor == len(e.text)

    def test_typing_afterwards_extends_the_tag(self):
        """The reported symptom: typing used to leave the highlight intact."""
        e, var = self._entry_with_selection()
        insert_tag_into_entry(e, var, _spec("bpm"))
        e.text = var.get()  # the textvariable drives the widget
        e.cursor = len(e.text)
        e.type_key("140")
        assert e.text == "freedom dive {bpm}:140"

    def test_backspace_afterwards_deletes_one_character(self):
        """The other half: backspace used to delete the whole highlight."""
        e, var = self._entry_with_selection()
        insert_tag_into_entry(e, var, _spec("bpm"))
        e.text = var.get()
        e.cursor = len(e.text)
        e.backspace()
        assert e.text == "freedom dive {bpm}"

    def test_the_old_behaviour_is_what_the_model_reproduces(self):
        """Guards the model itself: skip the clear and both symptoms return."""
        e = _FakeEntry("freedom dive", sel=(0, 7))
        e.text = "freedom dive {bpm}:"   # appended, selection deliberately kept
        e.cursor = len(e.text)
        e.type_key("140")
        assert e.text == "freedom dive {bpm}:140"  # selection not replaced
        e2 = _FakeEntry("freedom dive {bpm}:", sel=(0, 7))
        e2.cursor = len(e2.text)
        e2.backspace()
        assert e2.text == " dive {bpm}:"           # "freedom" wiped instead

    def test_works_with_no_selection(self):
        e = _FakeEntry("freedom dive")
        var = _Var("freedom dive")
        insert_tag_into_entry(e, var, _spec("bpm"))
        assert var.get() == "freedom dive {bpm}:"
        assert not e.selection_present()

    def test_survives_a_widget_that_cannot_clear(self):
        class _Stubborn(_FakeEntry):
            def select_clear(self):
                raise tk.TclError("invalid command name")

        e = _Stubborn("abc", sel=(0, 3))
        var = _Var("abc")
        insert_tag_into_entry(e, var, _spec("bpm"))
        assert var.get() == "abc {bpm}:"  # still appends


# ── Cutting ──────────────────────────────────────────────────────────────────

class TestCutFromEntry:
    def test_copies_and_removes_the_selection(self):
        e = _FakeEntry("hello world", sel=(0, 6))
        assert cut_from_entry(e) == "hello "
        assert e.clipboard == "hello "
        assert e.text == "world"

    def test_no_selection_changes_nothing(self):
        e = _FakeEntry("hello", clipboard="precious")
        assert cut_from_entry(e) == ""
        assert e.text == "hello"
        assert e.clipboard == "precious"

    def test_text_survives_a_clipboard_failure(self):
        """Never delete what we couldn't copy — that would just lose it."""

        class _NoClipboard(_FakeEntry):
            def clipboard_clear(self):
                raise tk.TclError("no clipboard")

        e = _NoClipboard("hello world", sel=(0, 6))
        assert cut_from_entry(e) == ""
        assert e.text == "hello world"

    def test_cut_then_paste_moves_the_text(self):
        src = _FakeEntry("{bpm}:<=140 psi", sel=(0, 11))
        moved = cut_from_entry(src)
        assert src.text == " psi"
        dest = _FakeEntry("")
        paste_into_entry(dest, moved)
        assert dest.text == "{bpm}:<=140"


# ── Key bindings ─────────────────────────────────────────────────────────────

class _BindRecorder(_FakeEntry):
    """Records bind() calls so the wiring can be inspected and invoked."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.bindings: dict = {}

    def bind(self, sequence, handler):
        self.bindings[sequence] = handler

    def select_range(self, first, last):
        self.sel = (first, len(self.text) if last == "end" else last)

    def clipboard_get(self):
        return self.clipboard


class _Var:
    def __init__(self, value=""):
        self._v = value

    def get(self):
        return self._v

    def set(self, v):
        self._v = v


@pytest.fixture
def bound():
    e = _BindRecorder("hello world")
    bind_tag_menu(e, _Var("hello world"))
    return e


class TestKeyBindings:
    @pytest.mark.parametrize("seq", [
        "<Button-3>",
        "<Control-x>", "<Control-X>",
        "<Control-c>", "<Control-C>",
        "<Control-v>", "<Control-V>",
        "<Control-a>", "<Control-A>",
    ])
    def test_sequence_is_bound(self, bound, seq):
        """Upper-case spellings included: Caps Lock sends Control-C, not -c."""
        assert seq in bound.bindings

    @pytest.mark.parametrize("seq", [
        "<Control-x>", "<Control-X>", "<Control-c>", "<Control-C>",
        "<Control-v>", "<Control-V>", "<Control-a>", "<Control-A>",
    ])
    def test_handlers_break_the_class_binding(self, bound, seq):
        """Without "break", the event keeps travelling the bindtag chain.

        Two things downstream would then also fire: Tk's own Entry class
        bindings for <<Cut>>/<<Copy>>/<<Paste>> (so a paste lands twice), and
        the enclosing window's shortcuts — SongBrowser binds Ctrl+A to
        select-all-songs, which must not steal Ctrl+A from the search box.
        """
        bound.sel = (0, 5)
        assert bound.bindings[seq](None) == "break"

    def test_ctrl_c_copies(self, bound):
        bound.sel = (0, 5)
        bound.bindings["<Control-c>"](None)
        assert bound.clipboard == "hello"
        assert bound.text == "hello world"

    def test_ctrl_x_cuts(self, bound):
        bound.sel = (0, 6)
        bound.bindings["<Control-x>"](None)
        assert bound.clipboard == "hello "
        assert bound.text == "world"

    def test_ctrl_v_pastes_over_the_selection(self, bound):
        bound.clipboard = "bye"
        bound.sel = (0, 5)
        bound.bindings["<Control-v>"](None)
        assert bound.text == "bye world"

    def test_ctrl_a_selects_everything(self, bound):
        bound.bindings["<Control-a>"](None)
        assert selected_text(bound) == "hello world"

    def test_copy_with_no_selection_is_harmless(self, bound):
        bound.clipboard = "precious"
        bound.sel = None
        assert bound.bindings["<Control-c>"](None) == "break"
        assert bound.clipboard == "precious"


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
