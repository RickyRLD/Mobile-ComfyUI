@echo off
setlocal

REM ============================================================
REM  test_tray.bat - Test menu tray
REM  Działa w dowolnym folderze dzięki ścieżkom bezwzględnym
REM ============================================================

REM Ścieżka do folderu z projektem Zdalne
set ZDALNE=%~dp0..

REM ============================================================
REM  ŚCIEŻKA DO PYTHONA - bezwzględna
REM ============================================================
set PYTHON=C:\AI\New_Comfy\python_embeded\python.exe

cd /d "%ZDALNE%"
"%PYTHON%" menedzer_tray.py
pause
