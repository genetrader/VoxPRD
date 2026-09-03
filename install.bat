@echo off
REM =====================================================================
REM  Voice Hotkey — install / setup
REM  - Creates a venv if missing
REM  - Installs deps into it
REM  - Migrates legacy top-level .env to secrets\.env (prompts first)
REM =====================================================================

setlocal
set "APP_DIR=%~dp0"
set "VENV=%APP_DIR%.venv"
set "PY=%VENV%\Scripts\python.exe"
set "PIP=%VENV%\Scripts\pip.exe"

echo.
echo  ===============================================
echo    VOICE HOTKEY - INSTALL
echo  ===============================================
echo.

REM --- Locate a Python interpreter ---
where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python is not on PATH. Install Python 3.11+ from python.org
  pause
  exit /b 1
)

REM --- Create venv if missing ---
if not exist "%VENV%\Scripts\python.exe" (
  echo [1/3] Creating venv at %VENV% ...
  python -m venv "%VENV%"
  if errorlevel 1 (
    echo [ERROR] venv creation failed
    pause
    exit /b 1
  )
) else (
  echo [1/3] venv already exists
)

REM --- Install deps ---
echo [2/3] Installing dependencies...
"%PIP%" install --upgrade pip >nul
"%PIP%" install pystray Pillow keyboard sounddevice soundfile requests openai-whisper
if errorlevel 1 (
  echo [ERROR] pip install failed
  pause
  exit /b 1
)

REM --- Migrate legacy .env into secrets\.env ---
echo [3/3] Checking secrets layout...
if not exist "%APP_DIR%secrets" mkdir "%APP_DIR%secrets"
if exist "%APP_DIR%.env" (
  if not exist "%APP_DIR%secrets\.env" (
    move "%APP_DIR%.env" "%APP_DIR%secrets\.env" >nul
    echo   Migrated .env ^-^> secrets\.env
  ) else (
    echo   secrets\.env already exists; left legacy .env alone
  )
)
if not exist "%APP_DIR%secrets\.env" (
  echo.
  echo  No .env found. Create one at:
  echo    %APP_DIR%secrets\.env
  echo  With at least:
  echo    DISCORD_BOT_TOKEN=...
  echo    OPENAI_API_KEY=...
  echo.
)

echo.
echo  ===============================================
echo    READY.
echo  ===============================================
echo  Run start.bat to launch.
echo  For silent auto-start, copy start-hidden.vbs to your Startup folder:
echo    Win+R -^> shell:startup
echo.
pause
endlocal
