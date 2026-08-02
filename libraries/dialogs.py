"""Standardized, dark-themed message dialogs.

All app message/error/confirmation boxes go through this module so they share
one look (Beat Saber dark theme, ``Warning.png`` title-bar icon, centered over
their parent) and one behavior (modal, keyboard-friendly).

The public functions deliberately mirror ``tkinter.messagebox`` so call sites
migrate with a straight rename:

    messagebox.showerror("Oops", msg)          -> dialogs.show_error("Oops", msg)
    messagebox.showinfo("Done", msg)           -> dialogs.show_info("Done", msg)
    messagebox.showwarning("Heads up", msg)    -> dialogs.show_warning("Heads up", msg)
    messagebox.askyesno("Q", msg, default=...) -> dialogs.ask_yes_no("Q", msg, default=...)
    messagebox.askokcancel("Q", msg)           -> dialogs.ask_ok_cancel("Q", msg)

``show_*`` return ``None``; ``ask_*`` return ``bool``, except ``ask_string``
(one line of text, or ``None`` when cancelled) and ``ask_custom`` (the chosen
button's value). Extra keyword arguments
accepted by ``messagebox`` (``icon=``, ``parent=``, ``default=``) are accepted
here too so existing calls keep working unchanged; severity is conveyed by a
colored glyph in the body regardless of any ``icon=`` value.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

import tkinter as tk

from libraries.constants import (
    ACCENT_COLOR,
    BG_COLOR,
    TEXT_COLOR,
)

# ── Palette (shared by every app dialog) ────────────────────────────────────
DIALOG_BG = BG_COLOR
ENTRY_BG = "#1e1e1e"
BTN_PRIMARY_BG = ACCENT_COLOR
BTN_PRIMARY_ACTIVE = "#a01d90"
BTN_SECONDARY_BG = "#2a2a3a"
BTN_SECONDARY_ACTIVE = "#3a3a4a"

_ERROR_COLOR = "#ff5c5c"
_WARN_COLOR = "#ffb454"
_INFO_COLOR = ACCENT_COLOR

# Severity -> (glyph, glyph color)
_GLYPHS = {
    "error": ("✕", _ERROR_COLOR),      # ✕
    "warning": ("⚠", _WARN_COLOR),     # ⚠
    "info": ("ℹ", _INFO_COLOR),        # ℹ
    "question": ("?", _INFO_COLOR),
}

_ICON_PATH = Path(__file__).resolve().parent.parent / "Warning.png"
# Cache the title-bar PhotoImage per Tk interpreter (keyed by root widget) so we
# don't re-decode the PNG on every dialog. PhotoImage is tied to its root, hence
# the per-root cache rather than a single global.
_icon_cache: "dict[object, tk.PhotoImage]" = {}


def _titlebar_icon(root: tk.Misc) -> Optional[tk.PhotoImage]:
    try:
        key = root._root()  # type: ignore[attr-defined]
    except Exception:
        key = None
    if key in _icon_cache:
        return _icon_cache[key]
    try:
        img = tk.PhotoImage(file=_ICON_PATH, master=root)
    except Exception:
        return None
    if key is not None:
        _icon_cache[key] = img
    return img


def _resolve_parent(parent: Optional[tk.Misc]) -> Optional[tk.Misc]:
    if parent is not None:
        return parent
    try:
        return tk._get_default_root()  # type: ignore[attr-defined]
    except Exception:
        return getattr(tk, "_default_root", None)


def themed_button(
    parent: tk.Misc,
    text: str,
    command=None,
    *,
    primary: bool = False,
    **kw,
) -> tk.Button:
    """A flat, theme-consistent button. ``primary`` uses the magenta accent;
    otherwise a muted secondary fill. Any keyword (``font``, ``padx`` …) can be
    overridden via ``**kw``."""
    opts = dict(
        font=("Segoe UI", 9),
        bg=BTN_PRIMARY_BG if primary else BTN_SECONDARY_BG,
        fg=TEXT_COLOR,
        activebackground=BTN_PRIMARY_ACTIVE if primary else BTN_SECONDARY_ACTIVE,
        activeforeground=TEXT_COLOR,
        bd=0,
        relief="flat",
        padx=16,
        pady=6,
        cursor="hand2",
    )
    opts.update(kw)
    return tk.Button(parent, text=text, command=command, **opts)


def themed_entry(parent: tk.Misc, **kw) -> tk.Entry:
    """A dark, flat text entry matching the app theme."""
    opts = dict(
        font=("Segoe UI", 10),
        bg=ENTRY_BG,
        fg=TEXT_COLOR,
        insertbackground=TEXT_COLOR,
        relief="flat",
        bd=4,
    )
    opts.update(kw)
    return tk.Entry(parent, **opts)


def selected_text(entry) -> str:
    """``entry``'s selected text, or ``""`` if nothing is selected.

    Read through ``sel.first``/``sel.last`` rather than ``selection_get()``,
    which reads the X PRIMARY selection and so can return another widget's
    text — or raise — when this entry isn't the selection owner.
    """
    try:
        if not entry.selection_present():
            return ""
        return entry.get()[entry.index("sel.first"):entry.index("sel.last")]
    except tk.TclError:
        return ""


def copy_from_entry(entry) -> str:
    """Put ``entry``'s selection on the clipboard. Returns what was copied.

    A no-op when nothing is selected: the menu disables Copy in that case, but
    a keyboard binding could still reach here with an empty selection, and
    clobbering the clipboard with "" would lose whatever the user had.
    """
    text = selected_text(entry)
    if not text:
        return ""
    try:
        entry.clipboard_clear()
        entry.clipboard_append(text)
    except tk.TclError:
        return ""
    return text


def cut_from_entry(entry) -> str:
    """Copy the selection to the clipboard, then delete it from ``entry``.

    Returns what was cut. Deletes only if the copy succeeded, so a clipboard
    failure can't silently eat the user's text.
    """
    text = copy_from_entry(entry)
    if not text:
        return ""
    try:
        entry.delete("sel.first", "sel.last")
    except tk.TclError:
        return ""
    entry.focus_set()
    return text


def paste_into_entry(entry, clipboard: str) -> None:
    """Insert ``clipboard`` at the cursor, replacing any selection.

    Tk's built-in ``<<Paste>>`` leaves the selection in place on some
    platforms, which turns a paste-over-all into an append. Doing the delete
    ourselves makes the behaviour the same everywhere.
    """
    if not clipboard:
        return
    try:
        if entry.selection_present():
            entry.delete("sel.first", "sel.last")
        entry.insert("insert", clipboard)
    except tk.TclError:
        return
    entry.focus_set()


def bind_clipboard_menu(entry, extend=None) -> None:
    """Give ``entry`` a Cut / Copy / Paste right-click menu and Ctrl keys.

    Cut and Copy are disabled with no selection and Paste with an empty
    clipboard, so the menu always describes what it will actually do.

    ``extend`` is an optional ``callable(menu)`` invoked after a separator,
    for callers that want more than the clipboard items; ``bind_tag_menu``
    uses it to hang the tag picker off the same menu. Left out, the menu is
    just the three clipboard entries — which is all a plain prompt like the
    YouTube-link dialog wants.
    """

    def _paste() -> None:
        try:
            text = entry.clipboard_get()
        except tk.TclError:
            return  # empty clipboard, or it holds something that isn't text
        paste_into_entry(entry, text)

    def _popup(event):
        menu = tk.Menu(entry, tearoff=0, bg=ENTRY_BG, fg=TEXT_COLOR,
                       activebackground=ACCENT_COLOR,
                       activeforeground=TEXT_COLOR, bd=0)
        try:
            can_paste = bool(entry.clipboard_get())
        except tk.TclError:
            can_paste = False
        has_sel = bool(selected_text(entry))
        menu.add_command(label="Cut", command=lambda: cut_from_entry(entry),
                         state="normal" if has_sel else "disabled")
        menu.add_command(label="Copy", command=lambda: copy_from_entry(entry),
                         state="normal" if has_sel else "disabled")
        menu.add_command(label="Paste", command=_paste,
                         state="normal" if can_paste else "disabled")
        # Held on the widget so Tk can't garbage-collect the menu out from
        # under the posted window while it is still on screen.
        entry._clipboard_menu = menu  # type: ignore[attr-defined]
        if extend is not None:
            menu.add_separator()
            extend(menu)
        menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def _select_all(_event=None):
        entry.select_range(0, "end")
        entry.icursor("end")
        return "break"

    entry.bind("<Button-3>", _popup)

    for seq, fn in (
        ("<Control-x>", cut_from_entry), ("<Control-X>", cut_from_entry),
        ("<Control-c>", copy_from_entry), ("<Control-C>", copy_from_entry),
    ):
        entry.bind(seq, lambda _e, f=fn: (f(entry), "break")[1])
    for seq in ("<Control-v>", "<Control-V>"):
        entry.bind(seq, lambda _e: (_paste(), "break")[1])
    for seq in ("<Control-a>", "<Control-A>"):
        entry.bind(seq, _select_all)


def themed_option_menu(
    parent: tk.Misc,
    textvariable: tk.StringVar,
    values: Sequence[str],
    command=None,
    **kw,
) -> tk.Menubutton:
    """A dark drop-down: pick one of ``values`` into ``textvariable``.

    A ``Menubutton`` + ``tk.Menu`` rather than a ``ttk.Combobox``. ttk widgets
    ignore ``bg``/``fg`` and can only be darkened through a named style plus
    the option database — process-wide state, including a ``theme_use`` call
    that changes ttk rendering for the whole interpreter. That is a lot of
    global reach for a short, fixed list of choices, and ``tk.Menu`` is
    already how every other menu in the app is themed.

    ``command`` is called with the chosen label, after the variable is set.
    """
    opts = dict(
        font=("Segoe UI", 9),
        bg=ENTRY_BG, fg=TEXT_COLOR,
        activebackground=BTN_SECONDARY_ACTIVE, activeforeground=TEXT_COLOR,
        relief="flat", bd=0, highlightthickness=0,
        anchor="w", padx=6, pady=3,
        indicatoron=True, cursor="hand2",
    )
    opts.update(kw)
    btn = tk.Menubutton(parent, textvariable=textvariable, **opts)
    menu = tk.Menu(btn, tearoff=0, bg=ENTRY_BG, fg=TEXT_COLOR,
                   activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR,
                   bd=0)

    def _choose(label: str) -> None:
        textvariable.set(label)
        if command is not None:
            command(label)

    for label in values:
        menu.add_command(label=label, command=lambda lbl=label: _choose(lbl))
    btn.configure(menu=menu)
    return btn


def _apply_icon(dlg: tk.Toplevel, parent: Optional[tk.Misc], icon) -> None:
    """Set ``dlg``'s title-bar icon. ``icon`` may be a PhotoImage, a path/str to
    a PNG, or None to inherit the parent window's icon."""
    img = None
    try:
        if isinstance(icon, tk.PhotoImage):
            img = icon
        elif icon is not None:
            img = tk.PhotoImage(file=icon, master=dlg)
        elif parent is not None:
            img = getattr(parent, "_icon", None)
    except Exception:
        img = None
    if img is not None:
        try:
            dlg.iconphoto(False, img)
            dlg._dialog_icon = img  # keep a reference alive
        except Exception:
            pass


def themed_toplevel(
    parent: tk.Misc,
    title: str,
    *,
    icon=None,
    modal: bool = True,
) -> tk.Toplevel:
    """Create a dark, non-resizable, modal ``Toplevel`` with a title-bar icon,
    ready for a caller to fill with custom widgets. Pair with ``center_over``
    after populating it."""
    dlg = tk.Toplevel(parent)
    dlg.title(title)
    dlg.configure(bg=DIALOG_BG)
    dlg.resizable(False, False)
    try:
        dlg.transient(parent.winfo_toplevel())
    except Exception:
        pass
    _apply_icon(dlg, parent, icon)
    if modal:
        dlg.grab_set()
    return dlg


def center_over(dlg: tk.Toplevel, parent: tk.Misc) -> None:
    """Position ``dlg`` centered over ``parent``'s top-level window, falling
    back to screen-center if the parent isn't mapped."""
    dlg.update_idletasks()
    try:
        anchor = parent.winfo_toplevel()
        if not anchor.winfo_viewable():
            raise RuntimeError
        x = anchor.winfo_rootx() + (anchor.winfo_width() - dlg.winfo_width()) // 2
        y = anchor.winfo_rooty() + (anchor.winfo_height() - dlg.winfo_height()) // 2
    except Exception:
        x = (dlg.winfo_screenwidth() - dlg.winfo_width()) // 2
        y = (dlg.winfo_screenheight() - dlg.winfo_height()) // 2
    dlg.geometry(f"+{max(x, 0)}+{max(y, 0)}")


def _make_button(
    frame: tk.Frame,
    text: str,
    command,
    *,
    primary: bool,
) -> tk.Button:
    return themed_button(frame, text, command, primary=primary)


def _run_dialog(
    *,
    title: str,
    message: str,
    severity: str,
    buttons: Sequence[tuple[str, object, bool]],
    parent: Optional[tk.Misc],
    default_value: object,
) -> object:
    """Build and show a modal themed dialog.

    ``buttons`` is a sequence of ``(label, return_value, is_primary)``. The
    dialog blocks until a button is pressed (or the window is closed, which
    yields ``default_value``) and returns the chosen ``return_value``.
    """
    master = _resolve_parent(parent)
    if master is None:
        # No Tk root at all — nothing we can render onto. Fail safe.
        return default_value

    result: dict[str, object] = {"value": default_value}

    dlg = tk.Toplevel(master)
    dlg.title(title)
    dlg.configure(bg=DIALOG_BG)
    dlg.resizable(False, False)
    dlg.transient(master.winfo_toplevel())

    icon = _titlebar_icon(master)
    if icon is not None:
        try:
            dlg.iconphoto(False, icon)
            dlg._dialog_icon = icon  # keep a reference alive
        except Exception:
            pass

    glyph, glyph_color = _GLYPHS.get(severity, _GLYPHS["info"])

    body = tk.Frame(dlg, bg=DIALOG_BG)
    body.pack(fill="both", expand=True, padx=24, pady=(22, 8))

    tk.Label(
        body,
        text=glyph,
        font=("Segoe UI", 26),
        bg=DIALOG_BG,
        fg=glyph_color,
    ).pack(side="left", anchor="n", padx=(0, 16))

    tk.Label(
        body,
        text=message,
        font=("Segoe UI", 10),
        bg=DIALOG_BG,
        fg=TEXT_COLOR,
        justify="left",
        wraplength=380,
    ).pack(side="left", anchor="n")

    btn_frame = tk.Frame(dlg, bg=DIALOG_BG)
    btn_frame.pack(padx=24, pady=(6, 20))

    def _choose(value: object) -> None:
        result["value"] = value
        dlg.destroy()

    default_btn: Optional[tk.Button] = None
    for label, value, is_primary in buttons:
        b = _make_button(
            btn_frame, label, (lambda v=value: _choose(v)), primary=is_primary
        )
        b.pack(side="left", padx=4)
        b.bind("<Return>", lambda _e, v=value: (_choose(v), "break")[1])
        if value == default_value and default_btn is None:
            default_btn = b

    # Keyboard: Enter activates the focused button (falling back to the
    # default when focus is elsewhere); Escape returns default_value.
    dlg.bind("<Return>", lambda _e: _choose(default_value))
    dlg.bind("<Escape>", lambda _e: _choose(default_value))
    dlg.protocol("WM_DELETE_WINDOW", lambda: _choose(default_value))

    dlg.update_idletasks()
    anchor = master.winfo_toplevel()
    try:
        if anchor.winfo_viewable():
            x = anchor.winfo_rootx() + (anchor.winfo_width() - dlg.winfo_width()) // 2
            y = anchor.winfo_rooty() + (anchor.winfo_height() - dlg.winfo_height()) // 2
        else:
            raise RuntimeError
    except Exception:
        # Fall back to screen center if the parent isn't mapped.
        x = (dlg.winfo_screenwidth() - dlg.winfo_width()) // 2
        y = (dlg.winfo_screenheight() - dlg.winfo_height()) // 2
    dlg.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    dlg.grab_set()
    if default_btn is not None:
        default_btn.focus_set()
    dlg.wait_window()
    return result["value"]


# ── Public API (mirrors tkinter.messagebox) ─────────────────────────────────

def show_info(
    title: str,
    message: str,
    *,
    parent: Optional[tk.Misc] = None,
    icon: Optional[str] = None,
    default: Optional[str] = None,
) -> None:
    _run_dialog(
        title=title, message=message, severity="info",
        buttons=[("OK", True, True)], parent=parent, default_value=True,
    )


def show_warning(
    title: str,
    message: str,
    *,
    parent: Optional[tk.Misc] = None,
    icon: Optional[str] = None,
    default: Optional[str] = None,
) -> None:
    _run_dialog(
        title=title, message=message, severity="warning",
        buttons=[("OK", True, True)], parent=parent, default_value=True,
    )


def show_error(
    title: str,
    message: str,
    *,
    parent: Optional[tk.Misc] = None,
    icon: Optional[str] = None,
    default: Optional[str] = None,
) -> None:
    _run_dialog(
        title=title, message=message, severity="error",
        buttons=[("OK", True, True)], parent=parent, default_value=True,
    )


def ask_yes_no(
    title: str,
    message: str,
    *,
    parent: Optional[tk.Misc] = None,
    icon: Optional[str] = None,
    default: Optional[str] = None,
) -> bool:
    """Yes/No confirmation. Returns ``True`` for Yes, ``False`` for No.

    ``default`` ("yes"/"no", as in messagebox) selects the focused button and
    the value used when the dialog is closed or Escape is pressed; it defaults
    to "no" for safety when unspecified.
    """
    default_value = False if (default or "no").lower() == "no" else True
    severity = "warning" if (icon or "").lower() == "warning" else "question"
    return bool(_run_dialog(
        title=title, message=message, severity=severity,
        buttons=[("Yes", True, True), ("No", False, False)],
        parent=parent, default_value=default_value,
    ))


def ask_ok_cancel(
    title: str,
    message: str,
    *,
    parent: Optional[tk.Misc] = None,
    icon: Optional[str] = None,
    default: Optional[str] = None,
) -> bool:
    """OK/Cancel confirmation. Returns ``True`` for OK, ``False`` for Cancel."""
    default_value = True if (default or "ok").lower() == "ok" else False
    severity = "warning" if (icon or "").lower() == "warning" else "question"
    return bool(_run_dialog(
        title=title, message=message, severity=severity,
        buttons=[("OK", True, True), ("Cancel", False, False)],
        parent=parent, default_value=default_value,
    ))


def ask_string(
    title: str,
    prompt: str,
    *,
    initial: str = "",
    parent: Optional[tk.Misc] = None,
    width: int = 46,
    ok_label: str = "OK",
) -> Optional[str]:
    """Prompt for one line of text. Returns the entry's contents, or ``None``
    if the dialog was cancelled, escaped or closed.

    ``_run_dialog`` can't do this — it has no entry field — so this builds on
    ``themed_toplevel``/``themed_entry`` directly. The returned string is
    stripped; both cancel and an empty entry are falsy, so callers that just
    want "did I get something" can test the result directly.

    ``initial`` is pre-selected, so typing replaces it and Ctrl+V/Enter is the
    whole interaction when the prefill is wrong.
    """
    master = _resolve_parent(parent)
    if master is None:
        return None

    result: dict[str, Optional[str]] = {"value": None}

    dlg = themed_toplevel(master, title)

    body = tk.Frame(dlg, bg=DIALOG_BG, padx=24, pady=18)
    body.pack(fill="both", expand=True)

    tk.Label(
        body, text=prompt, font=("Segoe UI", 10), bg=DIALOG_BG, fg=TEXT_COLOR,
        justify="left", wraplength=420, anchor="w",
    ).pack(fill="x", pady=(0, 10))

    entry = themed_entry(body, width=width)
    entry.insert(0, initial)
    entry.pack(fill="x")
    bind_clipboard_menu(entry)

    btn_frame = tk.Frame(dlg, bg=DIALOG_BG)
    btn_frame.pack(padx=24, pady=(12, 18))

    def _ok(_event=None):
        result["value"] = entry.get().strip()
        dlg.destroy()

    def _cancel(_event=None):
        dlg.destroy()

    themed_button(btn_frame, ok_label, _ok, primary=True).pack(side="left", padx=4)
    themed_button(btn_frame, "Cancel", _cancel).pack(side="left", padx=4)

    dlg.bind("<Return>", _ok)
    dlg.bind("<Escape>", _cancel)
    dlg.protocol("WM_DELETE_WINDOW", _cancel)

    center_over(dlg, master)
    entry.focus_set()
    entry.select_range(0, "end")
    dlg.wait_window()
    return result["value"]


def ask_custom(
    title: str,
    message: str,
    buttons: Sequence[tuple[str, object]],
    *,
    parent: Optional[tk.Misc] = None,
    default: object = "",
    severity: str = "question",
) -> object:
    """General multi-button dialog.

    ``buttons`` is a sequence of ``(label, return_value)``; the first is styled
    as primary. Returns the chosen value, or ``default`` if closed/escaped.
    """
    spec = [
        (label, value, i == 0)
        for i, (label, value) in enumerate(buttons)
    ]
    return _run_dialog(
        title=title, message=message, severity=severity,
        buttons=spec, parent=parent, default_value=default,
    )
