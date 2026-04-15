@echo off
echo Installing dependencies...
pip install -r requirements.txt

echo.
echo Starting Claude Code Bridge...
python claude_bridge.py
pause
