@echo off
chcp 65001 > nul
echo === NOTAM解析 ===
python main.py build-notam
pause