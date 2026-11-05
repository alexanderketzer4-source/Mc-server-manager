@echo off
REM One-click build: creates dist\MinecraftServerManager.exe (no Python needed to run it).
REM Requires Python 3.9+ installed on the build machine.
echo Installing dependencies...
python -m pip install --upgrade customtkinter requests pillow pyinstaller
if errorlevel 1 goto fail
echo Building exe...
python -m PyInstaller --noconfirm --onefile --windowed --name MinecraftServerManager --collect-all customtkinter main.py
if errorlevel 1 goto fail
echo.
echo Done! Your app is: dist\MinecraftServerManager.exe
pause
exit /b 0
:fail
echo Build failed - see messages above.
pause
exit /b 1
