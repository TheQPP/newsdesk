@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt
if not exist config.toml copy config.example.toml config.toml
echo.
echo Now open config.toml, paste your bot token and forum channel IDs, then run:
echo     python newsdesk.py --check
echo     python newsdesk.py --setup
echo     python newsdesk.py --test-post
echo     python newsdesk.py --dry-run
echo     run_newsdesk.bat
pause
