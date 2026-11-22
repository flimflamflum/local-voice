@echo off
rem Starts the Vesper phone server (shows the address + token for the Android app).
cd /d "%~dp0"
title Vesper phone server
".venv\Scripts\python.exe" server.py
pause
