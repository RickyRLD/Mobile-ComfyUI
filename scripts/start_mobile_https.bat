@echo off
setlocal

REM ============================================================
REM  start_mobile_https.bat - HTTPS Server
REM ============================================================

REM Ścieżka do folderu z projektem Zdalne (ten plik jest w scripts/)
set ZDALNE=%~dp0..

REM ============================================================
REM  ŚCIEŻKA DO PYTHONA - bezwzględna
REM ============================================================
set PYTHON=C:\AI\New_Comfy\python_embeded\python.exe

REM Ścieżka do certyfikatu - względna
set CERT=%ZDALNE%\cert.pem
set KEY=%ZDALNE%\key.pem

if not exist "%CERT%" (
    echo [!!] Brak certyfikatu SSL!
    echo      Uruchom najpierw: setup_https.bat
    pause
    exit /b 1
)

set LOCAL_IP=localhost
if exist "%ZDALNE%\local_ip.txt" (
    set /p LOCAL_IP=<"%ZDALNE%\local_ip.txt"
)

echo ============================================================
echo   ComfyUI Mobile - HTTPS
echo   Adres: https://%LOCAL_IP%:8001
echo ============================================================
echo.

cd /d "%ZDALNE%"
"%PYTHON%" -m pip install fastapi uvicorn python-multipart --quiet
"%PYTHON%" -m uvicorn serwer_comfy:app --host 0.0.0.0 --port 8001 --ssl-certfile "%CERT%" --ssl-keyfile "%KEY%"

pause
