"""
Playlist art preview window.

Shows the current playlist's cover image (sourced either from a
loaded .bplist's embedded image or from the first queued song's
cover). Accepts drag-and-drop and right-click "Replace…" / "Reset"
actions to override the art.
"""

from __future__ import annotations

import io
import json
import os
import base64
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import tkinter as tk
from tkinter import messagebox
from tkinterdnd2 import DND_FILES
from PIL import Image, ImageTk

from libraries.constants import ACCENT_COLOR, TEXT_COLOR
from libraries.song_data import compute_song_hash

if TYPE_CHECKING:
    from Browser import SongBrowser


def _show_queue_empty_warning(parent: tk.Misc) -> None:
    dlg = tk.Toplevel(parent)
    dlg.title("Queue Empty")
    dlg.configure(bg="#0d0d1a")
    dlg.resizable(False, False)
    dlg.transient(parent)
    dlg.grab_set()
    try:
        _ico = tk.PhotoImage(file=Path(__file__).parent.parent / "Warning.png")
        dlg.iconphoto(False, _ico)
        dlg._ico = _ico
    except Exception:
        pass
    tk.Label(
        dlg,
        text="Add at least one song to the queue first.",
        font=("Segoe UI", 10),
        bg="#0d0d1a", fg=TEXT_COLOR,
        padx=20, pady=16,
    ).pack()
    tk.Button(
        dlg, text="OK",
        font=("Segoe UI", 9),
        bg=ACCENT_COLOR, fg=TEXT_COLOR,
        activebackground="#7a44c0", activeforeground=TEXT_COLOR,
        bd=0, padx=14, pady=6,
        command=dlg.destroy,
    ).pack(pady=(0, 16))
    dlg.update_idletasks()
    x = parent.winfo_rootx() + (parent.winfo_width() - dlg.winfo_width()) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - dlg.winfo_height()) // 2
    dlg.geometry(f"+{x}+{y}")
    dlg.wait_window()


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

        self.bind("<Control-s>", self._save_queue_as_playlist)

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
        menu.add_command(label="Save Image…", command=self._save_art)
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

    def _save_queue_as_playlist(self, _event=None):
        import tkinter.filedialog as fd
        b = self._browser
        if not b._queue:
            _show_queue_empty_warning(self)
            return

        songs = list(b._queue)
        invalid = [s for s in songs if not s.song_hash]
        valid = [s for s in songs if s.song_hash]

        if invalid:
            names = "\n".join(f"  • {s.display_name}" for s in invalid)
            if not valid:
                messagebox.showerror(
                    "Cannot Create Playlist",
                    "None of the queued songs have a hash — they may not have been "
                    "loaded by Beat Saber yet.\n\n" + names,
                    parent=self,
                )
                return
            if not messagebox.askyesno(
                "Invalid Songs",
                f"{len(invalid)} song(s) have no hash and will be skipped:\n\n"
                + names
                + f"\n\nContinue with the remaining {len(valid)} song(s)?",
                parent=self,
            ):
                return

        # Detect songs whose Info.dat has been edited (a .bak backup exists).
        edited_baks: dict[Path, Path] = {}
        for s in valid:
            for bak_name in ("Info.dat.bak", "info.dat.bak", "INFO.DAT.bak"):
                bak = s.folder / bak_name
                if bak.exists():
                    edited_baks[s.folder] = bak
                    break

        if edited_baks:
            edited_names = "\n".join(
                f"  • {s.display_name}" for s in valid if s.folder in edited_baks
            )
            messagebox.showwarning(
                "Edited Songs Detected",
                f"{len(edited_baks)} song(s) have a modified Info.dat "
                f"(original backed up as .bak):\n\n{edited_names}\n\n"
                "Modifying Info.dat changes the SongCore hash used to identify and "
                "download songs — the edited version will not be recognised by "
                "other tools or players.\n\n"
                "The playlist will use a best-effort hash recalculated from the "
                "original Info.dat file.",
                parent=self,
            )

        # Build hash overrides from .bak originals for edited songs.
        hash_overrides: dict[Path, str] = {}
        for folder, bak in edited_baks.items():
            h = compute_song_hash(folder, bak)
            if h:
                hash_overrides[folder] = h

        save_path = fd.asksaveasfilename(
            title="Save Playlist",
            filetypes=[("Beat Saber Playlist", "*.bplist"), ("All files", "*.*")],
            defaultextension=".bplist",
            parent=self,
        )
        if not save_path:
            return

        playlist = {
            "playlistTitle": Path(save_path).stem,
            "playlistAuthor": "",
            "image": b._playlist_art_b64 or "",
            "customData": {},
            "songs": [
                {
                    "key": s.song_id,
                    "hash": hash_overrides.get(s.folder, s.song_hash),
                    "songName": s.display_name,
                }
                for s in valid
            ],
        }

        content = json.dumps(playlist, ensure_ascii=False, indent=2)
        target = Path(save_path)
        fd, tmp_str = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_str, target)
        except:
            Path(tmp_str).unlink(missing_ok=True)
            raise

        messagebox.showinfo(
            "Playlist Saved",
            f"Saved {len(valid)} songs to {target.name}",
            parent=self,
        )
