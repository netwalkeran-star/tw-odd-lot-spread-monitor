@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在啟動台股「整張 vs 零股」盤中價差即時監控伺服器...
start "" http://127.0.0.1:8765
python server.py
pause
