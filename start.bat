@echo off
echo Starting Ambot Converter Bot...
set BOT_TOKEN=%BOT_TOKEN%
set ADMIN_TELEGRAM_ID=%ADMIN_TELEGRAM_ID%
set DB_PATH=music.db

call venv\Scripts\activate
python bot.py
pause
