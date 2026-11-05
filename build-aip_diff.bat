@echo off
chcp 65001 > nul
echo === AIRACデータの分割・解析・DB格納 ===
python update_aip_diff.py
pause
