@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_exe\build.ps1"
if errorlevel 1 (echo. & echo BUILD FAILED)
pause
