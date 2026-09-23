@echo off
title Reddit-to-Instagram Autoposter Bot
echo ========================================================
echo   STARTING AUTOPOSTER DISCORD BOT
echo   Keep this window open while your PC is on.
echo   You can post anytime by typing /post in your Discord!
echo ========================================================
cd /d "%~dp0"
python main.py
pause
