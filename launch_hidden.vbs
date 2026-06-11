' Launches Meeting Assistant with no visible console window.
'
' Used by the "Launch at Startup" shortcut so nothing sits on the taskbar -
' the system tray icon is the only visible presence. Because the console is
' hidden, launcher + server output is redirected to storage\launch.log
' (overwritten on each launch). If the tray icon never appears, run
' launch.bat directly to see the error.
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = root

If Not fso.FolderExists(root & "\storage") Then fso.CreateFolder root & "\storage"

q = Chr(34)
bat = root & "\launch.bat"
logf = root & "\storage\launch.log"
sh.Run "cmd /c " & q & q & bat & q & " > " & q & logf & q & " 2>&1" & q, 0, False
