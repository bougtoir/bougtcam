# PyInstaller spec for BougtCam (Windows, folder distribution).
# Build with: pyinstaller bougtcam.spec --noconfirm
import os
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None
ffmpeg_bin = os.path.join("ffmpeg", "ffmpeg.exe")
binaries = [(ffmpeg_bin, "ffmpeg")] if os.path.exists(ffmpeg_bin) else []

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=binaries,
    datas=[],
    hiddenimports=collect_submodules("bougtcam"),
    hookspath=[],
    excludes=["tkinter"],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="BougtCam",
    debug=False,
    strip=False,
    upx=False,
    console=False,            # no console window
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False,
    name="BougtCam",
)
