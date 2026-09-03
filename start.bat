@echo off
REM =====================================================================
REM  Voice Hotkey — start (visible console for debugging)
REM =====================================================================
cd /d "%~dp0"
".venv\Scripts\python.exe" voice_hotkey.py
pause
