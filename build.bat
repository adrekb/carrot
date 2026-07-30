@echo off
echo ============================================
echo  Carrot Desktop App - Build Script
echo ============================================
echo.

echo [1/6] Installing Python dependencies...
pip install -e . --quiet

echo [2/6] Downloading bundled Ollama installer...
python scripts\build_installer.py

echo [3/6] Building offline editor bundles (Monaco, Milkdown)...
cd webvendor
call npm install --quiet
call npm run build
cd ..

echo [4/6] Installing Node.js dependencies...
cd gui
npm install --quiet
cd ..

echo [5/6] Building Electron desktop app...
cd gui
npm run dist
cd ..

echo [6/6] Done!
echo.
echo To run Carrot:
echo   - Web UI:    python -m carrot.app   (then open http://127.0.0.1:8181)
echo   - Desktop:   cd gui ^&^& npm start
echo   - CLI:       python -m carrot.main search "5 months ago bench press"
echo.
echo Installer output: gui\dist\Carrot Setup.exe
echo On first launch Carrot installs Ollama and pulls gemma4:e4b automatically.
echo.

pause
