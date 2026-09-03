' =====================================================================
' Voice Hotkey — silent auto-start launcher (idempotent + venv-pinned)
'
' Used by:
'   * Startup folder shortcut: %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\VoiceHotkey.lnk
'   * Manual double-click from anywhere
'
' Behavior:
'   * Kills any pre-existing voice_hotkey.py process
'   * Launches a fresh instance via the VENV pythonw (no console flash)
'   * Fully hidden — no UI
' =====================================================================
Option Explicit

Dim sh, fso, appDir, pyw, script, killCmd, runCmd
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

appDir = fso.GetParentFolderName(WScript.ScriptFullName)
pyw    = appDir & "\.venv\Scripts\pythonw.exe"
script = appDir & "\voice_hotkey.py"

If Not fso.FileExists(pyw) Then WScript.Quit 1
If Not fso.FileExists(script) Then WScript.Quit 1

' Kill any existing voice_hotkey instance
killCmd = "powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command " & _
          """Get-CimInstance Win32_Process -Filter \""Name='pythonw.exe' OR Name='python.exe'\"" | " & _
          "Where-Object { $_.CommandLine -like '*voice_hotkey.py*' } | " & _
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"""
sh.Run killCmd, 0, True

WScript.Sleep 800

sh.CurrentDirectory = appDir
runCmd = """" & pyw & """ """ & script & """"
sh.Run runCmd, 0, False
