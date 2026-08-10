@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" py -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
start "" http://127.0.0.1:5050
python app.py
pause
