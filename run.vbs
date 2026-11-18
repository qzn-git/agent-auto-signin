' ============================================
'  agent-auto-signin background launcher (no console)
'  Double-click to start Flask server on http://127.0.0.1:8765
'  Run stop.bat to stop
' ============================================
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
strDir = fso.GetParentFolderName(WScript.ScriptFullName)

' Kill stale old Flask processes first
On Error Resume Next
WshShell.Run "taskkill /F /FI ""WINDOWTITLE eq agent-auto-signin*"" /T", 0, True
On Error GoTo 0

WshShell.CurrentDirectory = strDir
' Start Python server hidden (no black window)
WshShell.Run chr(34) & strDir & "\.venv\Scripts\python.exe" & chr(34) & " server.py", 0, False

WScript.Echo "agent-auto-signin started in background" & vbCrLf & _
             "URL : http://127.0.0.1:8765/" & vbCrLf & _
             "Stop: run stop.bat"
