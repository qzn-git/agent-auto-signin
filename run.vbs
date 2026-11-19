' ============================================
'  agent-auto-signin silent launcher (no popup)
'  Double-click to start Flask server on http://127.0.0.1:8765
'  Run stop.bat to stop
' ============================================
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
strDir = fso.GetParentFolderName(WScript.ScriptFullName)

' Kill stale old Flask processes first (python.exe and pythonw.exe)
On Error Resume Next
WshShell.Run "taskkill /F /IM python.exe /FI ""WINDOWTITLE eq agent-auto-signin*"" /T", 0, True
WshShell.Run "taskkill /F /FI ""IMAGENAME eq pythonw.exe"" /FI ""MEMUSAGE gt 50000"" /T", 0, True
On Error GoTo 0

WshShell.CurrentDirectory = strDir
' Start Python server HIDDEN using pythonw.exe (no console, no window)
WshShell.Run chr(34) & strDir & "\.venv\Scripts\pythonw.exe" & chr(34) & " server.py", 0, False

' No WScript.Echo — silent start, no popup!
