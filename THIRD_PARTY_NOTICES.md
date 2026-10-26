# ImadeoCovert third-party notices

ImadeoCovert is derived from [cyber-anderson/Formatix](https://github.com/cyber-anderson/Formatix).
Original copyright: Copyright (C) 2026 cyber-anderson. Application code is
GPL-3.0-or-later; see `LICENSE`. The portable package includes the application
source snapshot under `source/` and its build identity in `build-info.json`.

## Media engine

The pinned Gyan FFmpeg 9.0.2 essentials build is GPLv3, with `--enable-gpl`
and `--enable-version3`. It is not an LGPL-only build. The unmodified provider
license, README, exact build configuration, source revision link, download URL
and executable hashes are retained in `tools/ffmpeg/` and
`tools/ffmpeg-manifest.json`.

- Provider: https://www.gyan.dev/ffmpeg/builds/
- Binary release: https://github.com/GyanD/codexffmpeg/releases/tag/9.0.2
- FFmpeg revision identified by the provider: https://github.com/FFmpeg/FFmpeg/commit/946fcce07b
- FFmpeg licensing information: https://ffmpeg.org/legal.html

The FFmpeg revision link alone does not identify all sources and build scripts
for its enabled external libraries. Public-release readiness is tracked in
`release-policy.json`; development artifacts must not be described as having
completed the corresponding-source audit.

## Python and image/GUI components

The builder copies license texts supplied by the installed, pinned packages into
`licenses/`, including Python, Tcl/Tk, TkDND, Pillow, pillow-heif, resvg-py,
tkinterdnd2 and the PyInstaller bootloader license/exception. Consult those
original texts for their terms. `build-info.json` records the exact versions.

In particular, pillow-heif's Python source license differs from the terms of its
binary wheel. Its supplied `LICENSES_bundled.txt` identifies libheif, libde265,
x265 and Windows runtime components. Do not describe the complete wheel as
BSD-only. Its corresponding-source and license compatibility review is included
in the public-release gate, together with resvg/native runtime dependencies.

No third-party authors endorse this application. All notices and license texts
remain with their respective components.
