@echo off
rem ---------------------------------------------------------------------------
rem One-click launcher: double-click this file to read the current price of
rem every target in targets.json and send the result to Discord / Telegram
rem (runs "python src/tracker.py --send-now").
rem
rem Unlike run_tracker.cmd this keeps the output in the window, so you see the
rem prices and any error straight away, and it waits for a key press before
rem closing. Set PT_NO_PAUSE=1 to skip that when calling it from a script.
rem ---------------------------------------------------------------------------
setlocal
chcp 65001 >nul
title Price Tracker - send prices now
cd /d "%~dp0"

rem UTF-8 for Python: the output has currency symbols and table characters that
rem would raise UnicodeEncodeError on a non-UTF-8 code page (e.g. Greek cp1253).
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [X] Python environment not found:
  echo     %PY%
  echo.
  echo     Create it once from this folder:
  echo         python -m venv .venv
  echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo         .venv\Scripts\python.exe -m playwright install chromium
  set "RC=2"
  goto :finish
)

echo Reading the current prices of every target (about 15 seconds)...
echo.

rem The tracker copies its own output into logs\tracker.log (TRACKER_LOG_FILE), so
rem this window stays live and the file survives after the window is closed.
rem logs\ is gitignored. No shell tee on purpose: PowerShell 5.1 writes UTF-16 and
rem re-parses the arguments, which corrupts the log and splits a two-word
rem "--target" value into two arguments.
if not exist "%~dp0logs" mkdir "%~dp0logs"
set "LOG=%~dp0logs\tracker.log"
set "TRACKER_LOG_FILE=%LOG%"

"%PY%" "src\tracker.py" --send-now %*
set "RC=%ERRORLEVEL%"

echo.
rem Exit code 3 means the prices were stored but the chat message was rejected.
if "%RC%"=="0" (
  echo Done - every message was accepted by the chat channel ^(see delivery above^).
) else if "%RC%"=="3" (
  echo The prices were SAVED but NOT delivered - exit code 3.
  echo The webhook is the usual cause: it must be one single line, with no
  echo spaces and no "DISCORD_WEBHOOK_URL=" in front of it.
  echo   local:  the DISCORD_WEBHOOK_URL line in .env
  echo   cloud:  the DISCORD_WEBHOOK_URL secret in the GitHub repo settings
) else (
  echo Something went wrong, exit code %RC% - see the messages above.
  echo Common causes: no network, a changed Cloudflare challenge, or a missing
  echo DISCORD_WEBHOOK_URL / TELEGRAM_* entry in .env.
)
echo Full output: %LOG%

:finish
if not defined PT_NO_PAUSE pause
exit /b %RC%
