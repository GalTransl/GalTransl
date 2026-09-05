@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\activate.bat" (
  call ".venv\Scripts\activate.bat"
)

if exist "%USERPROFILE%\.cargo\bin\cargo.exe" (
  set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
)

where python >nul 2>nul
if errorlevel 1 (
  echo Python not found in PATH.
  pause
  exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
  echo npm not found in PATH.
  pause
  exit /b 1
)

if not exist "desktop\package.json" (
  echo desktop\package.json not found.
  pause
  exit /b 1
)

if not exist "desktop\node_modules" (
  echo Installing desktop frontend dependencies...
  call npm --prefix desktop install
  if errorlevel 1 (
    echo Failed to install desktop dependencies.
    pause
    exit /b 1
  )
)

start "GalTransl Backend" cmd /k python run_backend.py --host 127.0.0.1 --port 12333

where cargo >nul 2>nul
if errorlevel 1 (
  echo Cargo not found. Falling back to browser frontend dev server.
  start "GalTransl Frontend" /D "%~dp0desktop" cmd /k npm run dev
) else (
  start "GalTransl Desktop" /D "%~dp0desktop" cmd /k npm run tauri:dev
)

echo Backend and desktop frontend are starting in separate windows.
echo If Cargo is installed, the Tauri desktop shell will start.
echo Otherwise the browser frontend will start at the Vite dev URL.
pause
