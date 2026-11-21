@echo off
rem Starts the Jarvis phone server (shows the address + token for the Android app).
cd /d "%~dp0"
title Jarvis phone server
".venv\Scripts\python.exe" server.py
pause
