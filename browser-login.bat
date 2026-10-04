@echo off
cd /d "%~dp0"

rem Prefer the project venv: that is where playwright + migate live.
rem Setup:  python -m venv .venv
rem         .venv\Scripts\pip install playwright migate
rem         .venv\Scripts\python -m playwright install chromium
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo ============================================================
echo   Xiaomi login via real browser  (recommended)
echo.
echo   A browser window will open. Sign in to i.mi.com there.
echo   Finish any security verification in that window.
echo   After it succeeds, cookies are saved automatically and
echo   the backend can refresh them silently from now on.
echo ============================================================
echo.

"%PY%" -m notesync.browser_auth

echo.
echo ------------------------------------------------------------
echo Done. Exit code = %ERRORLEVEL%
echo You can close this window and refresh the web page.
echo ------------------------------------------------------------
pause
