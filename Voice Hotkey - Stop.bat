@echo off
REM Stops any running Voice Hotkey instance.
title Voice Hotkey - Stop
color 0E
echo.
echo  Stopping Voice Hotkey...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | Where-Object { $_.CommandLine -like '*voice_hotkey.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
echo  Done.
timeout /t 1 /nobreak >nul
exit /b 0
