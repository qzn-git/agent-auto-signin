Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
strDir = fso.GetParentFolderName(WScript.ScriptFullName)
strPy  = strDir & "\.venv\Scripts\python.exe"

' 1. 杀旧进程（只杀 venv 下的 python.exe）
On Error Resume Next
WshShell.Run "wmic process where ""name='python.exe' and ExecutablePath like '%agent-auto-signin%"" call terminate", 0, True
On Error GoTo 0

' 2. 后台 Flask
WshShell.CurrentDirectory = strDir
WshShell.Run chr(34) & strPy & chr(34) & " -u -B server.py", 0, False

' 3. 进度条（HTML 里自己 resizeTo 缩到最小）
WshShell.Run "mshta.exe """ & strDir & "\run_progress.html""", 1, False

WScript.Quit