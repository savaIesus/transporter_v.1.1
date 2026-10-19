@echo off
setlocal
cd /d "%~dp0"

rem GPU / CUDA: when an NVIDIA driver is detected we install the CUDA build of
rem torch on top of the CPU build that requirements.txt pulls in together with
rem Ultralytics. Both torch and torchvision must be reinstalled together with
rem --force-reinstall --no-deps: reinstalling torch alone leaves a torchvision
rem wheel that no longer matches the installed torch and the import fails.
rem The wheel index is set by TORCH_CUDA_INDEX, default cu128.

where py >nul 2>&1
if errorlevel 1 goto no_launcher

py -3.11 -m venv .venv
if errorlevel 1 goto no_python311

if not exist ".venv\Scripts\python.exe" goto no_python311

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto pip_failed

".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto pip_failed

if not "%TORCH_CUDA_INDEX%"=="" goto cuda_index_ready
set "TORCH_CUDA_INDEX=cu128"

:cuda_index_ready
nvidia-smi >nul 2>&1
if errorlevel 1 goto cpu_setup

echo NVIDIA driver found - installing CUDA PyTorch [%TORCH_CUDA_INDEX%] ...
".venv\Scripts\python.exe" -m pip install --force-reinstall --no-deps torch torchvision --index-url https://download.pytorch.org/whl/%TORCH_CUDA_INDEX%
if errorlevel 1 echo WARN: CUDA PyTorch install failed - continuing with CPU PyTorch.
goto setup_done

:cpu_setup
echo NVIDIA driver not found - CPU PyTorch will be used.

:setup_done
echo Setup complete.
endlocal
exit /b 0

:no_launcher
echo ERROR: the "py" launcher was not found in PATH.
echo Install Python 3.11 from https://www.python.org/downloads/ and enable
echo the "py launcher" option, then run setup.bat again.
echo.
pause
endlocal
exit /b 1

:no_python311
echo ERROR: could not create .venv with Python 3.11.
echo Check that Python 3.11 is installed: py -3.11 --version
echo.
pause
endlocal
exit /b 1

:pip_failed
echo ERROR: dependency installation failed - see the pip output above.
echo.
pause
endlocal
exit /b 1
