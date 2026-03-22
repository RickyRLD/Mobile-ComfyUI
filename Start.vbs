Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)

' Pełna ścieżka do Pythona z ComfyUI
pythonPath = "C:\AI\New_Comfy\python_embeded\python.exe"
scriptPath = base & "\menedzer_tray.py"

Set WshShell = CreateObject("WScript.Shell")

' Uruchom bez okna CMD (0 = ukryte, False = nie czekaj na zakończenie)
WshShell.Run """" & pythonPath & """ """ & scriptPath & """", 0, False
