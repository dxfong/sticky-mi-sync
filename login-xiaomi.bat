@echo off
cd /d "%~dp0"
chcp 65001 >nul

rem Pick the project venv first: that is where migate lives.
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo ============================================================
echo   Xiaomi account login for sticky-mi-sync
echo   Log in once here; the backend refreshes it automatically.
echo ============================================================
echo.

"%PY%" -m notesync.mi_login --verify

echo.
echo ------------------------------------------------------------
echo Done. You can close this window and refresh the web page.
echo ------------------------------------------------------------
pause
