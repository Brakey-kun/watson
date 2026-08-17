@echo off
setlocal enabledelayedexpansion

:: =============================================================================
:: Watson OSINT Workbench - Portable Launcher (Windows)
:: =============================================================================
:: Self-healing portable launcher that:
::   1. Checks Python version >= 3.10
::   2. Creates/recreates virtual environment as needed
::   3. Detects folder relocation via portable_root_marker
::   4. Bootstraps pip if missing
::   5. Installs dependencies from requirements.txt
::   6. Launches the application
:: =============================================================================

:: Store the directory where this script lives as Portable_Root
set "PORTABLE_ROOT=%~dp0"
:: Remove trailing backslash for clean path comparisons
if "%PORTABLE_ROOT:~-1%"=="\" set "PORTABLE_ROOT=%PORTABLE_ROOT:~0,-1%"

:: ---------------------------------------------------------------------------
:: Step 1: Check Python version >= 3.10
:: ---------------------------------------------------------------------------
echo [*] Checking Python version...

:: Try to find python on PATH
where python >nul 2>&1
if errorlevel 1 (
    echo [-] Error: Python was not found on the system PATH.
    echo [-] Please install Python 3.10 or higher and ensure it is on your PATH.
    exit /b 1
)

:: Get Python version string
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set "PY_VERSION=%%v"

:: Parse major and minor version numbers
for /f "tokens=1,2 delims=." %%a in ("%PY_VERSION%") do (
    set "PY_MAJOR=%%a"
    set "PY_MINOR=%%b"
)

:: Validate version >= 3.10
if not defined PY_MAJOR (
    echo [-] Error: Could not determine Python version.
    exit /b 1
)

if %PY_MAJOR% LSS 3 (
    echo [-] Error: Python 3.10 or higher is required. Found Python %PY_VERSION%.
    exit /b 1
)

if %PY_MAJOR% EQU 3 (
    if %PY_MINOR% LSS 10 (
        echo [-] Error: Python 3.10 or higher is required. Found Python %PY_VERSION%.
        exit /b 1
    )
)

echo [+] Python %PY_VERSION% detected.

:: ---------------------------------------------------------------------------
:: Step 2: Check for virtual environment and relocation detection
:: ---------------------------------------------------------------------------
set "VENV_DIR=%~dp0.venv"
set "MARKER_FILE=%VENV_DIR%\portable_root_marker"
set "NEED_RECREATE=0"

if exist "%VENV_DIR%" (
    :: Virtual environment exists - check for relocation
    if exist "%MARKER_FILE%" (
        :: Read stored path from marker file
        set /p STORED_PATH=<"%MARKER_FILE%"
        :: Compare with current Portable_Root
        if /i not "!STORED_PATH!"=="%PORTABLE_ROOT%" (
            echo [*] Folder relocation detected.
            echo     Previous location: !STORED_PATH!
            echo     Current location:  %PORTABLE_ROOT%
            set "NEED_RECREATE=1"
        )
    ) else (
        :: No marker file found - recreate to be safe
        echo [*] No portable_root_marker found. Recreating virtual environment...
        set "NEED_RECREATE=1"
    )
) else (
    :: No .venv directory at all
    set "NEED_RECREATE=1"
)

:: ---------------------------------------------------------------------------
:: Step 3: Create or recreate virtual environment if needed
:: ---------------------------------------------------------------------------
if %NEED_RECREATE% EQU 1 (
    :: Remove existing .venv if present (relocation case)
    if exist "%VENV_DIR%" (
        echo [*] Removing old virtual environment...
        rmdir /s /q "%VENV_DIR%"
        if errorlevel 1 (
            echo [-] Error: Failed to remove old virtual environment.
            echo [-] Please manually delete the .venv directory and try again.
            exit /b 1
        )
    )

    echo [*] Creating virtual environment...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [-] Error: Failed to create virtual environment.
        echo [-] Ensure Python 3.10+ is installed correctly and has venv support.
        exit /b 1
    )
    echo [+] Virtual environment created successfully.

    :: Write current path to portable_root_marker
    echo %PORTABLE_ROOT%>"%MARKER_FILE%"
    echo [+] Portable root marker written.
)

:: ---------------------------------------------------------------------------
:: Step 4: Bootstrap pip if not available
:: ---------------------------------------------------------------------------
echo [*] Checking pip availability...
"%VENV_DIR%\Scripts\python.exe" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [*] pip not found. Bootstrapping with ensurepip...
    "%VENV_DIR%\Scripts\python.exe" -m ensurepip --default-pip
    if errorlevel 1 (
        echo [-] Error: Failed to bootstrap pip using ensurepip.
        echo [-] The virtual environment may be corrupted. Try deleting .venv and running again.
        exit /b 1
    )
    echo [+] pip bootstrapped successfully.
) else (
    echo [+] pip is available.
)

:: ---------------------------------------------------------------------------
:: Step 5: Install dependencies from requirements.txt
:: ---------------------------------------------------------------------------
echo [*] Installing/verifying requirements...
if not exist "%~dp0requirements.txt" (
    echo [-] Error: requirements.txt not found in Portable_Root.
    echo [-] The application cannot start without its dependency list.
    exit /b 1
)

"%VENV_DIR%\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt" --quiet
if errorlevel 1 (
    echo [-] Error: Failed to install requirements from requirements.txt.
    echo [-] Check your network connection and try again.
    echo [-] You can also try manually: .venv\Scripts\python.exe -m pip install -r requirements.txt
    exit /b 1
)
echo [+] Dependencies installed successfully.

:: ---------------------------------------------------------------------------
:: Step 6: Launch the application
:: ---------------------------------------------------------------------------
echo [*] Launching Watson OSINT Workbench...
"%VENV_DIR%\Scripts\python.exe" "%~dp0gui.py"
if errorlevel 1 (
    echo [-] Error: Application exited with an error.
    exit /b 1
)

endlocal
