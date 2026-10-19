@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto calibrate

echo Python environment not found: .venv
echo Run setup.bat first.
echo.
pause
exit /b 1

:calibrate
".venv\Scripts\python.exe" -m vision.camera_calibration_console --config camera_mapping.json
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
