@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto run

echo Python environment not found: .venv
echo Run setup.bat first.
pause
exit /b 1

:run
".venv\Scripts\python.exe" -m training --open-browser %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
endlocal & exit /b %EXIT_CODE%
