@echo off
rem ---------------------------------------------------------------------------
rem Local runner for the price tracker (used by Task Scheduler or by hand).
rem   run_tracker.cmd                 -> real run, alerts sent
rem   run_tracker.cmd --dry-run       -> extra arguments are passed through
rem Output is appended to logs\tracker.log; the script exits with the
rem tracker's own exit code (0 = ok, 1 = a target failed, 2 = config error).
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

rem Force UTF-8 so the currency symbols and separators in the output never
rem raise UnicodeEncodeError on a non-UTF-8 Windows code page (e.g. cp1253).
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist "logs" mkdir "logs"

echo.>> "logs\tracker.log"
echo ===== run started %DATE% %TIME% =====>> "logs\tracker.log"

".venv\Scripts\python.exe" "src\tracker.py" %* >> "logs\tracker.log" 2>&1
set "RC=%ERRORLEVEL%"

echo ===== run finished %DATE% %TIME% (exit %RC%) =====>> "logs\tracker.log"

exit /b %RC%
