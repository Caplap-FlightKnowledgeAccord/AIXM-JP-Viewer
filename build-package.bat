@echo off
chcp 65001 > nul
echo === Build Server Package for Linux (Ubuntu) ===
echo.
python main.py package
if %errorlevel% neq 0 (
    echo [ERROR] Package build failed.
    pause
    exit /b 1
)
echo.
echo [OK] Package created: dist\aixm-jp-viewer-server.tar.gz
echo.
pause
