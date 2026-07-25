' Silent source launcher for Windows — no console window.
' Double-click after the venv exists, or use start.bat (also installs deps).
' Packaged release: use OCIBot.exe instead.
Option Explicit
Dim shell, fso, root, pythonw, app, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
If Right(root, 1) <> "\" Then root = root & "\"
pythonw = root & ".venv\Scripts\pythonw.exe"
app = root & "main.py"
If Not fso.FileExists(pythonw) Then
  MsgBox "开发环境尚未创建，请先运行 start.bat。" & vbCrLf & "发行版请直接双击 OCIBot.exe。", 16, "OCIBot"
  WScript.Quit 1
End If
If Not fso.FileExists(app) Then
  MsgBox "找不到 main.py：" & vbCrLf & app, 16, "OCIBot"
  WScript.Quit 1
End If
command = Chr(34) & pythonw & Chr(34) & " " & Chr(34) & app & Chr(34)
shell.CurrentDirectory = Left(root, Len(root) - 1)
' IMPORTANT: style 0 hides the process window — including the Tk GUI.
' Use 1 so the app window is visible; pythonw still has no console.
shell.Run command, 1, False
