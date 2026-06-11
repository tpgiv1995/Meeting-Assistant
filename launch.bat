@echo off
setlocal
set "ROOT=%~dp0"
set "VENV=%ROOT%.venv"

:: ── Ensure uv is available ───────────────────────────────────────────────────
set "UV="
where uv >nul 2>&1
if not errorlevel 1 (
    set "UV=uv"
    goto :have_uv
)

:: Check common install location (installer adds to user PATH but not current session)
if exist "%USERPROFILE%\.local\bin\uv.exe" (
    set "UV=%USERPROFILE%\.local\bin\uv.exe"
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
    goto :have_uv
)

:: Install uv via the official installer
echo.
echo  Installing uv package manager...
echo.
powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

where uv >nul 2>&1
if not errorlevel 1 (
    set "UV=uv"
    goto :have_uv
)
if exist "%USERPROFILE%\.local\bin\uv.exe" (
    set "UV=%USERPROFILE%\.local\bin\uv.exe"
    goto :have_uv
)

echo.
echo  Failed to install uv. Please install manually:
echo  https://docs.astral.sh/uv/getting-started/installation/
echo.
pause & exit /b 1

:have_uv

:: ── Create venv if needed (uv auto-downloads Python 3.12 if not found) ──────
if not exist "%VENV%\Scripts\python.exe" (
    echo  Creating Python environment...
    %UV% venv "%VENV%" --python 3.12 --seed
    if errorlevel 1 (
        echo  Failed to create virtual environment.
        pause & exit /b 1
    )
)

"%VENV%\Scripts\python.exe" "%ROOT%launch.py"
if errorlevel 1 pause
