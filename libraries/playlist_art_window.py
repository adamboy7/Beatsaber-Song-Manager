"""
Playlist art preview window.

Shows the current playlist's cover image (sourced either from a
loaded .bplist's embedded image or from the first queued song's
cover). Accepts drag-and-drop and right-click "Replace…" / "Reset"
actions to override the art.
"""

from __future__ import annotations

import io
import base64
from pathlib import Path
from typing import TYPE_CHECKING

import tkinter as tk
from tkinter import messagebox
from tkinterdnd2 import DND_FILES
from PIL import Image, ImageTk

from libraries.constants import ACCENT_COLOR, TEXT_COLOR

if TYPE_CHECKING:
    from Browser import SongBrowser


class PlaylistArtWindow(tk.Toplevel):
    _IMG_SIZE = 400

    def __init__(self, browser: "SongBrowser"):
        super().__init__(browser)
        self._browser = browser
        self._photo: ImageTk.PhotoImage | None = None

        self.title("Playlist Art")
        self.configure(bg="#0d0d1a")
        self.resizable(False, False)
        _icon = tk.PhotoImage(file=Path(__file__).parent.parent / "Album.png")
        self.iconphoto(False, _icon)
        self._icon = _icon

        self._lbl = tk.Label(self, bg="#0d0d1a", cursor="hand2")
        self._lbl.pack(padx=16, pady=16)
        self._lbl.bind("<Button-3>", self._on_right_click)
        self._lbl.drop_target_register(DND_FILES)
        self._lbl.dnd_bind('<<Drop>>', self._on_drop)
        self._lbl.dnd_bind('<<DropEnter>>', self._on_drop_enter)
        self._lbl.dnd_bind('<<DropLeave>>', self._on_drop_leave)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.refresh()

    def _on_close(self):
        self._browser._playlist_art_window = None
        self.destroy()

    def refresh(self):
        b64 = self._browser._playlist_art_b64
        size = self._IMG_SIZE
        try:
            if b64:
                img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
            else:
                img = Image.new("RGB", (size, size), "#2a0033")
            img = img.resize((size, size), Image.LANCZOS)
            self._photo = ImageTk.PhotoImage(img)
            self._lbl.config(image=self._photo)
        except Exception:
            img = Image.new("RGB", (size, size), "#2a0033")
            self._photo = ImageTk.PhotoImage(img)
            self._lbl.config(image=self._photo)

    def _on_right_click(self, event: tk.Event):
        menu = tk.Menu(
            self, tearoff=0,
            bg="#1e1e1e", fg=TEXT_COLOR,
            activebackground=ACCENT_COLOR, activeforeground=TEXT_COLOR, bd=0,
        )
        menu.add_command(label="Replace…", command=self._replace_art)
        menu.add_command(label="Save as…", command=self._save_art)
        menu.add_command(label="Clear Art", command=self._reset_art)
        menu.tk_popup(event.x_root, event.y_root)

    def _reset_art(self):
        self._browser._playlist_art_locked = False
        self._browser._playlist_art_first_song_key = None
        self._browser._playlist_art_b64 = None
        self._browser._update_playlist_art_auto()
        self.refresh()

    def _on_drop_enter(self, _event):
        self.configure(bg=ACCENT_COLOR)
        self._lbl.config(bg=ACCENT_COLOR)

    def _on_drop_leave(self, _event):
        self.configure(bg="#0d0d1a")
        self._lbl.config(bg="#0d0d1a")

    def _on_drop(self, event):
        self.configure(bg="#0d0d1a")
        self._lbl.config(bg="#0d0d1a")
        path = self.tk.splitlist(event.data)[0]
        try:
            buf = io.BytesIO()
            Image.open(path).convert("RGB").save(buf, format="JPEG")
            self._browser._playlist_art_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            self._browser._playlist_art_locked = True
        except Exception as e:
            messagebox.showerror("Error", f"Could not load image:\n{e}", parent=self)
            return
        self.refresh()

    def _save_art(self):
        import tkinter.filedialog as fd
        b64 = self._browser._playlist_art_b64
        if not b64:
            messagebox.showinfo("No image", "There is no image to save.", parent=self)
            return
        path = fd.asksaveasfilename(
            title="Save Image As",
            defaultextension=".jpg",
            filetypes=[
                ("JPEG", "*.jpg *.jpeg"),
                ("PNG", "*.png"),
                ("All files", "*.*"),
            ],
            parent=self,
        )
        if not path:
            return
        try:
            img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
            fmt = "PNG" if Path(path).suffix.lower() == ".png" else "JPEG"
            img.save(path, format=fmt)
        except Exception as e:
            messagebox.showerror("Error", f"Could not save image:\n{e}", parent=self)

    def _replace_art(self):
        import tkinter.filedialog as fd
        path = fd.askopenfilename(
            title="Select Image",
            filetypes=[
                ("Image files", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"),
                ("All files", "*.*"),
            ],
            parent=self,
        )
        if not path:
            return
        try:
            buf = io.BytesIO()
            Image.open(path).convert("RGB").save(buf, format="JPEG")
            self._browser._playlist_art_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            self._browser._playlist_art_locked = True
        except Exception as e:
            messagebox.showerror("Error", f"Could not load image:\n{e}", parent=self)
            return
        self.refresh()
