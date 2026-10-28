@echo off
REM Flow Bulk Generator - one-click setup. Double-click this file.
REM Everything of substance lives in install.ps1, which is tested; this
REM wrapper only exists so the installer can be started with a double-click.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -Bootstrap
echo.
pause
