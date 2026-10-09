@echo off
REM BougtCam build script for Windows 11.
REM Usage: build\build.bat   (run from repo root; requires Python 3.11+ on PATH)
setlocal enabledelayedexpansion
cd /d "%~dp0\.."

where python >nul 2>&1 || (echo [ERR] python not found on PATH & exit /b 1)

if not exist .venv (
  python -m venv .venv || exit /b 1
)
call .venv\Scripts\activate.bat

python -m pip install --upgrade pip || exit /b 1
pip install -r requirements.txt || exit /b 1

REM --- fetch ffmpeg (release essentials, GPL build w/ libx264) ---
if not exist ffmpeg\ffmpeg.exe (
  echo [..] downloading ffmpeg release essentials
  mkdir ffmpeg 2>nul
  powershell -NoProfile -Command ^
    "$u='https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip';" ^
    "Invoke-WebRequest -Uri $u -OutFile 'ffmpeg\\ff.zip';" ^
    "Expand-Archive -Path 'ffmpeg\\ff.zip' -DestinationPath 'ffmpeg\\x' -Force;" ^
    "$d=Get-ChildItem 'ffmpeg\\x' -Directory | Select-Object -First 1;" ^
    "Copy-Item (Join-Path $d.FullName 'bin\\ffmpeg.exe') 'ffmpeg\\ffmpeg.exe';" ^
    "Remove-Item 'ffmpeg\\ff.zip'; Remove-Item 'ffmpeg\\x' -Recurse -Force" || exit /b 1
)

pyinstaller bougtcam.spec --noconfirm --clean || exit /b 1

echo [OK] dist\BougtCam\BougtCam.exe
endlocal
