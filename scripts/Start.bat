@echo off
setlocal

REM ============================================================
REM  Start.bat - Uniwerssalny skrypt startowy
REM ============================================================

REM Ścieżka do folderu z projektem Zdalne (ten plik jest w scripts/)
set ZDALNE=%~dp0..

REM Ścieżka do certyfikatu - względna
set CERT=%ZDALNE%\cert.pem

REM ============================================================
REM  ŚCIEŻKA DO ComfyUI - bezwzględna
REM ============================================================
set COMFY=C:\AI\New_Comfy

if exist "%CERT%" (
    echo [HTTPS] Certyfikat SSL znaleziony - tryb HTTPS
    start cmd /k "cd /d %ZDALNE%\scripts && call start_mobile_https.bat"
) else (
    echo [HTTP]  Brak certyfikatu - tryb HTTP
    echo         Aby wlaczyc HTTPS uruchom: setup_https.bat
    start cmd /k "cd /d %ZDALNE%\scripts && call start_mobile.bat"
)

echo Uruchamianie ComfyUI...
cd /d "%COMFY%"
if exist "run_nvidia_gpu — NEW.bat" (
    call "run_nvidia_gpu — NEW.bat"
) else if exist "run_nvidia_gpu.bat" (
    call "run_nvidia_gpu.bat"
) else (
    echo [BLAD] Nie znaleziono pliku startowego ComfyUI w folderze: %COMFY%
    echo Dostepne pliki .bat w folderze ComfyUI:
    dir /b *.bat
)
