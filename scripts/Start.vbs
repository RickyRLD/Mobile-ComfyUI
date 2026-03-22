Set WshShell = CreateObject("WScript.Shell")
' Podmień folder, gdzie zapisałeś menedzer_tray.py
WshShell.Run "cmd /c cd /d C:\AI\Zdalne && C:\AI\New_Comfy\python_embeded\python.exe menedzer_tray.py", 0, False