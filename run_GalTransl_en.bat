@echo off
chcp 65001
set "CURRENT_PATH=%CD%"
cd /d "%~dp0"
set "GT_LANG=en"
python run_GalTransl.py %*
pause
cd /d "%CURRENT_PATH%"