@echo off
setlocal

set "ROOT=%~dp0"
set "PYTHON_EXE=%ROOT%.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
  echo [ERROR] Python executable not found at "%PYTHON_EXE%".
  echo [ERROR] Create and install dependencies into .venv first.
  exit /b 1
)

pushd "%ROOT%"

echo [1/2] Installing Nuitka build dependencies...
"%PYTHON_EXE%" -m pip install nuitka ordered-set zstandard || goto :error

echo [2/2] Building standalone onefile executable...
"%PYTHON_EXE%" -m nuitka ^
  --onefile ^
  --standalone ^
  --assume-yes-for-downloads ^
  --enable-plugin=tk-inter ^
  --include-package-data=matplotlib ^
  --include-package-data=seaborn ^
  --include-package=customtkinter ^
  --output-dir=build\nuitka ^
  compile_transactions.py || goto :error

echo.
echo Build complete.
echo Executable: build\nuitka\compile_transactions.exe
popd
exit /b 0

:error
echo.
echo Build failed.
popd
exit /b 1
