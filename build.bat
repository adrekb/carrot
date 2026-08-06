@echo off
echo ============================================
echo  Carrot Desktop App - Build Script (Windows)
echo ============================================
echo.

echo [1/3] Installing Python dependencies...
pip install -e . --quiet
pip install pyinstaller --quiet

echo [2/3] Building one-click installer (backend freeze + Electron)...
python scripts\build_installer.py

echo [3/3] Done!
echo.
echo Installer output: gui\dist\Carrot Setup 0.3.0.exe
echo On first launch Carrot installs Ollama (CUDA + ROCm included) and
echo pulls the model chosen on the setup splash.
echo.

pause
