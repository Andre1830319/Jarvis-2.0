' Jarvis.vbs — starts jarvis.py completely hidden, with no terminal/console
' window at all (uses pythonw.exe instead of python.exe). Works wherever the
' jarvis-main folder lives, since it locates itself automatically.
'
' Usage:
'   - Double-click this file any time to start Jarvis silently.
'   - To start automatically every time you log into Windows: create a
'     SHORTCUT to this .vbs file (do not move/copy the .vbs itself) and
'     place that shortcut in your Startup folder (Win+R -> shell:startup).
'     See README.md for the full steps.

Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = scriptDir
shell.Run "pythonw.exe " & Chr(34) & scriptDir & "\jarvis.py" & Chr(34), 0, False
