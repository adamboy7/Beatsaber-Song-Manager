# -*- mode: python ; coding: utf-8 -*-
# Linux build spec. Mirrors Browser.spec but swaps the Windows-only pieces:
#   • pynput's X11 backend hidden imports instead of _win32
#   • no embedded .ico (PyInstaller can't embed one on Linux; the window icon
#     is still set at runtime from Icon.ico via PIL, which reads .ico fine)
# ffmpeg and libmpv are NOT bundled — install them via your package manager or
# drop them next to the binary (see Linux.md / README).
from PyInstaller.utils.hooks import collect_all

# Collect tkinterdnd2's shared libs and TCL scripts (needed for drag-and-drop).
datas_dnd, bins_dnd, hiddens_dnd = collect_all("tkinterdnd2")

a = Analysis(
    ["Browser.py"],
    pathex=[],
    binaries=bins_dnd,
    datas=[
        ("Icon.ico", "."),
        ("Album.png", "."),
        ("Playlist.png", "."),
        ("Warning.png", "."),
        ("Random.png", "."),
        ("Visualizer.png", "."),
    ] + datas_dnd,
    hiddenimports=hiddens_dnd + [
        # pynput's Linux backends power the global media-key listener; on
        # Wayland this may no-op, which the app tolerates gracefully.
        "pynput.keyboard._xorg",
        "pynput.mouse._xorg",
        "pynput.keyboard._dummy",
        "pynput.mouse._dummy",
        "audioop",
        "mpv",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["ffmpeg", "ffprobe"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="BeatSaberSongManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # No icon= on Linux: PyInstaller doesn't embed .ico into an ELF. The app
    # sets its window icon at runtime from Icon.ico.
)
