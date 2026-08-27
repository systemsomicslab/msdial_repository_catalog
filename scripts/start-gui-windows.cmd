@echo off
setlocal
set "APP_ROOT=%~dp0.."
set "PYTHONPATH=%APP_ROOT%\src;%PYTHONPATH%"
if defined MSDIAL_REPOSITORY_CATALOG (
  set "DATABASE=%MSDIAL_REPOSITORY_CATALOG%"
) else (
  set "DATABASE=%APP_ROOT%\catalog-data\catalog.sqlite"
)

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  py -3 -m msdial_repository_catalog.gui_server --database "%DATABASE%"
) else (
  python -m msdial_repository_catalog.gui_server --database "%DATABASE%"
)

if %ERRORLEVEL% NEQ 0 pause
