@echo off
setlocal
cd /d %~dp0

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

start "GalTransl Backend" cmd /k python run_backend.py --host 127.0.0.1 --port 18000

where cargo >nul 2>nul
if errorlevel 1 (
  echo Cargo not found. Falling back to browser frontend dev server.
  start "GalTransl Frontend" cmd /k "cd /d %~dp0desktop && npm run dev"
) else (
  start "GalTransl Desktop" cmd /k "cd /d %~dp0desktop && npm run tauri:dev"
)

echo Backend and desktop frontend are starting in separate windows.
echo If Cargo is installed, the Tauri desktop shell will start.
echo Otherwise the browser frontend will start at the Vite dev URL.
pause
