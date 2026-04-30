#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ -x "$ROOT/.venv/Scripts/python.exe" ]]; then
  PYTHON_EXE="$ROOT/.venv/Scripts/python.exe"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON_EXE="$ROOT/.venv/bin/python"
else
  echo "[ERROR] Python executable not found in .venv."
  echo "[ERROR] Expected one of:"
  echo "        $ROOT/.venv/Scripts/python.exe"
  echo "        $ROOT/.venv/bin/python"
  echo "[ERROR] Create and install dependencies into .venv first."
  exit 1
fi

cd "$ROOT"

echo "[1/2] Installing Nuitka build dependencies..."
"$PYTHON_EXE" -m pip install nuitka ordered-set zstandard

echo "[2/2] Building standalone onefile executable..."
"$PYTHON_EXE" -m nuitka \
  --onefile \
  --standalone \
  --assume-yes-for-downloads \
  --enable-plugin=tk-inter \
  --include-package-data=matplotlib \
  --include-package-data=seaborn \
  --include-package=customtkinter \
  --output-dir=build/nuitka \
  compile_transactions.py

echo
echo "Build complete."
echo "Executable: build/nuitka/compile_transactions.exe"
