@echo off
setlocal
set PYTHONUTF8=1
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "xinpianchang.py" %*
    goto finished
)
where python >nul 2>nul
if not errorlevel 1 (
    python "xinpianchang.py" %*
    goto finished
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "xinpianchang.py" %*
    goto finished
)
echo Python was not found. Install Python 3.10+ from https://www.python.org/downloads/
echo Enable "Add python.exe to PATH" during installation.
:finished
echo.
pause
