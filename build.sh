#!/usr/bin/env bash
# Linux build. Mirrors build.bat for the Windows spec.
# Prereqs: python3-tk on the system, plus `pip install -r requirements.txt pyinstaller`.
set -euo pipefail
cd "$(dirname "$0")"
pyinstaller Browser.linux.spec --noconfirm
echo "Built dist/BeatSaberSongManager — ensure ffmpeg and libmpv are installed or beside it."
