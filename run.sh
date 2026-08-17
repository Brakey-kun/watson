#!/bin/bash
# Watson OSINT Workbench - Portable Launcher (Linux/macOS)
# This script bootstraps the application environment:
#   - Checks Python version >= 3.10
#   - Creates/validates virtual environment
#   - Detects folder relocation via portable_root_marker
#   - Bootstraps pip if needed
#   - Installs dependencies
#   - Launches the application

set -e

# Resolve the directory where this script lives (Portable_Root)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR=".venv"
MARKER_FILE="$VENV_DIR/portable_root_marker"
REQUIREMENTS_FILE="requirements.txt"
APP_ENTRY="gui.py"

# --- Step 1: Find a suitable Python interpreter (>= 3.10) ---
echo "[*] Checking Python environment..."

PYTHON_CMD=""

# Try python3 first, then python
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        # Check version
        VERSION_OUTPUT=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
        if [ $? -eq 0 ]; then
            MAJOR=$(echo "$VERSION_OUTPUT" | cut -d. -f1)
            MINOR=$(echo "$VERSION_OUTPUT" | cut -d. -f2)
            if [ "$MAJOR" -gt 3 ] || ([ "$MAJOR" -eq 3 ] && [ "$MINOR" -ge 10 ]); then
                PYTHON_CMD="$cmd"
                break
            fi
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    echo "[-] Error: Python 3.10 or higher is required but was not found."
    echo "    Please install Python 3.10+ and ensure it is available on your PATH."
    echo "    Tried: python3, python"
    exit 1
fi

echo "[+] Found Python: $PYTHON_CMD ($VERSION_OUTPUT)"

# --- Step 2: Virtual environment setup and relocation detection ---
NEED_CREATE_VENV=0

if [ ! -d "$VENV_DIR" ]; then
    echo "[*] Virtual environment not found. Will create a new one."
    NEED_CREATE_VENV=1
elif [ -f "$MARKER_FILE" ]; then
    # Check for relocation: compare stored path with current path
    STORED_PATH=$(cat "$MARKER_FILE" 2>/dev/null)
    CURRENT_PATH="$SCRIPT_DIR"
    if [ "$STORED_PATH" != "$CURRENT_PATH" ]; then
        echo "[*] Folder relocation detected."
        echo "    Previous location: $STORED_PATH"
        echo "    Current location:  $CURRENT_PATH"
        echo "[*] Removing old virtual environment..."
        rm -rf "$VENV_DIR"
        NEED_CREATE_VENV=1
    fi
else
    # Marker file missing but .venv exists — treat as needing recreation
    echo "[*] Portable root marker missing. Recreating virtual environment."
    rm -rf "$VENV_DIR"
    NEED_CREATE_VENV=1
fi

# --- Step 3: Create virtual environment if needed ---
if [ "$NEED_CREATE_VENV" -eq 1 ]; then
    echo "[*] Creating virtual environment..."
    "$PYTHON_CMD" -m venv "$VENV_DIR"
    if [ $? -ne 0 ]; then
        echo "[-] Error: Failed to create virtual environment."
        echo "    Ensure Python 3.10+ has the 'venv' module available."
        echo "    On Debian/Ubuntu, you may need: sudo apt install python3-venv"
        exit 1
    fi
    echo "[+] Virtual environment created successfully."
fi

# --- Step 4: Write portable root marker ---
if [ "$NEED_CREATE_VENV" -eq 1 ]; then
    echo "$SCRIPT_DIR" > "$MARKER_FILE"
    echo "[+] Portable root marker updated."
fi

# --- Step 5: Bootstrap pip if not available ---
VENV_PYTHON="$VENV_DIR/bin/python"

if ! "$VENV_PYTHON" -m pip --version &>/dev/null; then
    echo "[*] pip not found in virtual environment. Bootstrapping with ensurepip..."
    "$VENV_PYTHON" -m ensurepip --default-pip
    if [ $? -ne 0 ]; then
        echo "[-] Error: Failed to bootstrap pip using ensurepip."
        echo "    The virtual environment may be corrupted. Try deleting .venv and re-running."
        exit 1
    fi
    echo "[+] pip bootstrapped successfully."
fi

# --- Step 6: Install dependencies from requirements.txt ---
if [ ! -f "$REQUIREMENTS_FILE" ]; then
    echo "[-] Error: $REQUIREMENTS_FILE not found."
    echo "    This file is required for the application to function."
    echo "    Please restore or recreate $REQUIREMENTS_FILE in the application directory."
    exit 1
fi

echo "[*] Installing/verifying requirements..."
"$VENV_PYTHON" -m pip install -r "$REQUIREMENTS_FILE" --quiet
if [ $? -ne 0 ]; then
    echo "[-] Error: Failed to install requirements from $REQUIREMENTS_FILE."
    echo "    Check your network connection and try again."
    echo "    You can also try manually: $VENV_PYTHON -m pip install -r $REQUIREMENTS_FILE"
    exit 1
fi
echo "[+] Dependencies installed successfully."

# --- Step 7: Launch the application ---
echo "[*] Launching Watson OSINT Workbench..."
"$VENV_PYTHON" "$APP_ENTRY"
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo "[-] Error: Application exited with code $EXIT_CODE."
    exit $EXIT_CODE
fi
