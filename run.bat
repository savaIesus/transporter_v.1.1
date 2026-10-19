@echo off
setlocal
cd /d "%~dp0"

rem Deliberately no parenthesised if-blocks here: an unescaped closing paren
rem inside such a block terminates it early, cmd then reports the rest of the
rem line as an unexpected token and aborts the script before Python starts.

if exist ".venv\Scripts\python.exe" goto run

echo Python environment not found: .venv
echo Run setup.bat first: it creates .venv and installs all dependencies,
echo including the CUDA build of PyTorch when an NVIDIA driver is present.
echo.
pause
exit /b 1

:run
".venv\Scripts\python.exe" main.py
set "EXIT_CODE=%ERRORLEVEL%"
if "%EXIT_CODE%"=="0" goto ok

echo.
echo main.py stopped with exit code %EXIT_CODE%.
pause
endlocal & exit /b %EXIT_CODE%

:ok
endlocal & exit /b 0
