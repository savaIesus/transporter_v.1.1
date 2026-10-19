@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto check

echo Python environment not found: .venv
echo Create it with: py -3.11 -m venv .venv
echo Then install: .venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
pause
exit /b 1

:check
".venv\Scripts\python.exe" -m vision.camera_diagnostic %*
set "EXIT_CODE=%ERRORLEVEL%"
if "%EXIT_CODE%"=="0" goto finish

echo.
echo Camera problems detected - see the report above.
pause

:finish
endlocal & exit /b %EXIT_CODE%
