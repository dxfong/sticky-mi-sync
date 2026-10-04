@echo off
cd /d "%~dp0"

rem Python 优先级：
rem   1) 项目内的 .venv —— 建了它就在里面装 migate，小米侧能自动续期
rem   2) PATH 里的 python
rem 建 venv（二选一，推荐）：
rem   python -m venv .venv
rem   .venv\Scripts\pip install -r requirements-optional.txt
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo ============================================================
echo   sticky-mi-sync : Microsoft Sticky Notes ^<--^> Xiaomi Notes
echo   URL : http://127.0.0.1:8787
echo   Close this window to stop the service.
echo ============================================================
echo.

"%PY%" -c "import migate" 2>nul
if errorlevel 1 (
  echo [note] migate is NOT installed. Xiaomi side will need a manually
  echo        pasted cookie, and you must re-paste it when it expires.
  echo        To enable auto-refresh:  "%PY%" -m pip install migate
  echo.
)

"%PY%" server.py --port 8787

echo.
echo ------------------------------------------------------------
echo Server exited. Error code = %ERRORLEVEL%
echo If there are errors above, copy the whole output and send it back.
echo ------------------------------------------------------------
pause
