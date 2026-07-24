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

``show_*`` return ``None``; ``ask_*`` return ``bool``. Extra keyword arguments
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
    SUBTEXT_COLOR,
    TEXT_COLOR,
)

# ── Palette ────────────────────────────────────────────────────────────────
_DIALOG_BG = BG_COLOR
_BTN_SECONDARY_BG = "#2a2a3a"
_BTN_ACTIVE_BG = "#7a44c0"
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


def _make_button(
    frame: tk.Frame,
    text: str,
    command,
    *,
    primary: bool,
) -> tk.Button:
    return tk.Button(
        frame,
        text=text,
        font=("Segoe UI", 9),
        bg=ACCENT_COLOR if primary else _BTN_SECONDARY_BG,
        fg=TEXT_COLOR,
        activebackground=_BTN_ACTIVE_BG,
        activeforeground=TEXT_COLOR,
        bd=0,
        padx=16,
        pady=6,
        cursor="hand2",
        command=command,
    )


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
    dlg.configure(bg=_DIALOG_BG)
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

    body = tk.Frame(dlg, bg=_DIALOG_BG)
    body.pack(fill="both", expand=True, padx=24, pady=(22, 8))

    tk.Label(
        body,
        text=glyph,
        font=("Segoe UI", 26),
        bg=_DIALOG_BG,
        fg=glyph_color,
    ).pack(side="left", anchor="n", padx=(0, 16))

    tk.Label(
        body,
        text=message,
        font=("Segoe UI", 10),
        bg=_DIALOG_BG,
        fg=TEXT_COLOR,
        justify="left",
        wraplength=380,
    ).pack(side="left", anchor="n")

    btn_frame = tk.Frame(dlg, bg=_DIALOG_BG)
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
        if value == default_value and default_btn is None:
            default_btn = b

    # Keyboard: Enter activates the default button, Escape returns default_value.
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
