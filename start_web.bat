@echo off
chcp 65001 > nul
echo === AIP/NOTAM 可視化システム Webサーバー起動 ===
python main.py web
pause
