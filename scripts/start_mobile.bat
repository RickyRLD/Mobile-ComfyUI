@echo off
setlocal

REM ============================================================
REM  start_mobile.bat - HTTP Server
REM ============================================================

REM Ścieżka do folderu z projektem Zdalne
set ZDALNE=%~dp0..

REM ============================================================
REM  ŚCIEŻKA DO PYTHONA - bezwzględna
REM ============================================================
set PYTHON=C:\AI\New_Comfy\python_embeded\python.exe

echo ============================================================
echo   ComfyUI Mobile - HTTP
echo   Adres: http://localhost:8001
echo ============================================================
echo.

cd /d "%ZDALNE%"
"%PYTHON%" -m pip install fastapi uvicorn python-multipart --quiet
"%PYTHON%" -m uvicorn serwer_comfy:app --host 0.0.0.0 --port 8001

pause
