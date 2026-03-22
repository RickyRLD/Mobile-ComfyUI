@echo off
setlocal
cd /d "%~dp0"
call "%~dp0scripts\start_mobile_https.bat" %*
