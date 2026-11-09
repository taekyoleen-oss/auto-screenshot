@echo off
cd /d "%~dp0"
pip install pyinstaller Pillow
pyinstaller --onefile --windowed --name AutoScreenshot auto_screenshot.py
echo.
echo dist\AutoScreenshot.exe 생성 완료
pause
