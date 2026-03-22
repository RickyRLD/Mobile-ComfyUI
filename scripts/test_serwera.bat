@echo off
setlocal

REM ============================================================
REM  test_serwera.bat - Test serwera
REM  Działa w dowolnym folderze dzięki ścieżkom bezwzględnym
REM ============================================================

REM Ścieżka do folderu z projektem Zdalne
set ZDALNE=%~dp0..

REM ============================================================
REM  ŚCIEŻKA DO PYTHONA - bezwzględna
REM ============================================================
set PYTHON=C:\AI\New_Comfy\python_embeded\python.exe

cd /d "%ZDALNE%"
"%PYTHON%" -m uvicorn serwer_comfy:app --host 0.0.0.0 --port 8001
pause
