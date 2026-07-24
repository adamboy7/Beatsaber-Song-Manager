# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

# Collect tkinterdnd2's DLLs and TCL scripts (needed for drag-and-drop).
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
        "pynput.keyboard._win32",
        "pynput.mouse._win32",
        "audioop",
        "mpv",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # ffmpeg/ffprobe and libmpv-2.dll are not bundled — users place them next
    # to the EXE or add them to PATH.
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
    icon="Icon.ico",
)
