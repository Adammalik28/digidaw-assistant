' Starts Digidaw without a console window (the HUD is the interface).
' Extra arguments are passed on, e.g.  wscript start_digidaw.vbs --background
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
args = ""
For Each a In WScript.Arguments
    args = args & " " & a
Next
shell.CurrentDirectory = dir
shell.Run """" & dir & "\.venv\Scripts\pythonw.exe"" """ & dir & "\main.py""" & args, 0, False
