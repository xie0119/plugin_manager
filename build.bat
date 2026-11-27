@echo off
setlocal
cd /d "%~dp0"
if exist "build\packaging-venv\Scripts\python.exe" (
  "build\packaging-venv\Scripts\python.exe" -B tools\build_installer.py
) else (
  python -B tools\build_installer.py
)
if errorlevel 1 (
  echo Build failed. See the error above.
  pause
  exit /b 1
)
echo Installer is ready in dist.
pause
