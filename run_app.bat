@echo off
cd /d "%~dp0"
echo 강남 호스텔 시장조사 웹앱을 실행합니다...
".venv\Scripts\streamlit.exe" run app.py
pause
