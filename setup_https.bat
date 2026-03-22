@echo off
setlocal
cd /d "%~dp0"
call "%~dp0scripts\setup_https.bat" %*
