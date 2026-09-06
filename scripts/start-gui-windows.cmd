@echo off
setlocal
title MS-DIAL Repository Catalog
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-gui-windows.ps1" %*
if errorlevel 1 pause
