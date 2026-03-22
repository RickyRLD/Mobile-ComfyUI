@echo off
setlocal enabledelayedexpansion

echo ============================================================
echo   SETUP HTTPS - ComfyUI Mobile
echo   Działa w dowolnym folderze dzięki ścieżkom bezwzględnym
echo ============================================================
echo.

REM Ścieżka do folderu z projektem Zdalne
set ZDALNE=%~dp0..

REM ============================================================
REM  ŚCIEŻKA DO PYTHONA - bezwzględna
REM ============================================================
set PYTHON=C:\AI\New_Comfy\python_embeded\python.exe

set MKCERT=%ZDALNE%\mkcert.exe
set CERT=%ZDALNE%\cert.pem
set KEY=%ZDALNE%\key.pem

cd /d "%ZDALNE%"

:: 1. Pobierz mkcert
if exist "%MKCERT%" (
    echo [OK] mkcert.exe znaleziony
) else (
    echo [..] Pobieranie mkcert.exe...
    powershell -Command "Invoke-WebRequest -Uri 'https://github.com/FiloSottile/mkcert/releases/download/v1.4.4/mkcert-v1.4.4-windows-amd64.exe' -OutFile '%MKCERT%'" 2>nul
    if not exist "%MKCERT%" (
        echo [!!] BLAD pobierania mkcert.exe
        echo      Pobierz recznie z github.com/FiloSottile/mkcert/releases
        echo      Zapisz jako: %MKCERT%
        pause
        exit /b 1
    )
    echo [OK] mkcert.exe pobrano
)

:: 2. Zainstaluj Root CA
echo.
echo [..] Instalowanie Root CA (moze pojawic sie UAC)...
"%MKCERT%" -install
if %errorlevel% neq 0 (
    echo [!!] BLAD instalacji Root CA
    pause
    exit /b 1
)
echo [OK] Root CA zainstalowany

:: 3. Wykryj lokalny IP
echo.
echo [..] Wykrywam IP...
set LOCAL_IP=
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /C:"IPv4" ^| findstr /V "127.0.0.1"') do (
    set TMPIP=%%a
    set TMPIP=!TMPIP: =!
    if not "!TMPIP!"=="" if "!LOCAL_IP!"=="" set LOCAL_IP=!TMPIP!
)
if "%LOCAL_IP%"=="" (
    echo [!!] Nie znaleziono IP automatycznie
    set /p LOCAL_IP="Wpisz lokalny IP (np. 192.168.1.100): "
)
echo [OK] IP: %LOCAL_IP%

:: 4. Generuj certyfikat
echo.
echo [..] Generowanie certyfikatu...
"%MKCERT%" -cert-file "%CERT%" -key-file "%KEY%" %LOCAL_IP% localhost 127.0.0.1
if %errorlevel% neq 0 (
    echo [!!] BLAD generowania certyfikatu
    pause
    exit /b 1
)
echo [OK] cert.pem i key.pem gotowe

:: 5. Zapisz IP
echo %LOCAL_IP%> "%ZDALNE%\local_ip.txt"
echo [OK] IP zapisano

:: 6. Skopiuj Root CA dla iPhone
echo.
for /f "usebackq tokens=*" %%a in (`"%MKCERT%" -CAROOT`) do set CAROOT=%%a
if exist "%CAROOT%\rootCA.pem" (
    copy "%CAROOT%\rootCA.pem" "%ZDALNE%\rootCA_dla_iPhone.pem" >nul
    echo [OK] rootCA_dla_iPhone.pem gotowy
)

echo.
echo ============================================================
echo   GOTOWE! Adres: https://%LOCAL_IP%:8000
echo ============================================================
echo.
echo   Na iPhonie (jednorazowo):
echo   1. Wyslij rootCA_dla_iPhone.pem na iPhone (AirDrop/email)
echo   2. Ustawienia - Pobrane profile - Zainstaluj
echo   3. Ustawienia - Ogolne - Informacje - Zaufanie certyfikatow
echo      Wlacz pelne zaufanie dla mkcert
echo   4. Dodaj apke do ekranu glownego od nowa z https://
echo.
pause