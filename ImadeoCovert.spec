# PyInstaller one-directory build. Build using scripts/build_portable.py.
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH)
dnd_data, dnd_binaries, dnd_hidden = collect_all('tkinterdnd2')
a = Analysis(
    [str(root / 'imadeo.py')], pathex=[str(root)],
    binaries=dnd_binaries, datas=[(str(root / 'icon.ico'), '.')] + dnd_data,
    hiddenimports=dnd_hidden, hookspath=[], runtime_hooks=[],
    excludes=['pytest', 'unittest', 'pip', 'setuptools', 'numpy', 'matplotlib', 'IPython'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name='ImadeoCovert', debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False, icon=str(root / 'icon.ico'),
    contents_directory='_internal',
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='ImadeoCovert')
