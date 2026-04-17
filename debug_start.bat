@echo off
echo ============================================
echo  Flight Case System - Diagnostika spusteni
echo ============================================
echo.

echo Kontrola Python...
python --version
if errorlevel 1 (
    echo CHYBA: Python nenalezen!
    pause
    exit /b
)

echo.
echo Kontrola Flask...
python -c "import flask; print('Flask OK:', flask.__version__)"
if errorlevel 1 (
    echo Flask neni nainstalovan. Instaluji...
    pip install flask
)

echo.
echo Kontrola databaze...
python -c "import os; db='data\\system.db'; print('DB existuje:', os.path.exists(db), '|', os.path.getsize(db), 'bytes') if os.path.exists(db) else print('DB neexistuje!')"

echo.
echo Spoustim server...
echo Po spusteni otevri: http://localhost:5000
echo Pro zastaveni stiskni CTRL+C
echo.
python app.py
if errorlevel 1 (
    echo.
    echo SERVER SELHAL! Viz chyba vyse.
)
pause
