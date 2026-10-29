@echo off
rem Build KbFix.exe  (requires Python 3.10+ installed with "Add python.exe to PATH")
cd /d "%~dp0"
echo === Installing build tools ===
python -m pip install --upgrade pip
python -m pip install keyboard mouse pyperclip pystray pillow pyinstaller || goto :fail
echo === Creating icon ===
python make_icon.py || goto :fail
echo === Building KbFix.exe ===
python -m PyInstaller --noconfirm --clean --onefile --noconsole --name KbFix --icon icon.ico --hidden-import pystray._win32 kbfix_app.py || goto :fail
echo.
echo DONE: dist\KbFix.exe
pause
exit /b 0
:fail
echo.
echo BUILD FAILED - see messages above
pause
exit /b 1
