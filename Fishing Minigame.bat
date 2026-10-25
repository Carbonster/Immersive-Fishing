@echo off
cd /d "%~dp0"
pymhf run fishing_minigame_mod.py
set "exit_code=%errorlevel%"
if not "%exit_code%"=="0" (
    echo.
    echo Fishing Minigame Mod failed to start or exited with an error. Exit code: %exit_code%
    echo Check the logs folder for additional details.
    pause
)
exit /b %exit_code%
