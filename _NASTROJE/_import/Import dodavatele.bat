@echo off
chcp 65001 >nul
cd /d "%~dp0\..\.."

echo ============================================
echo  Import dodavatelu z MATERIAL.csv
echo ============================================
echo.
echo Nejprve zastav Flask server (Ctrl+C v okne serveru).
echo Pak stiskni Enter pro spusteni importu.
pause
echo.
python importuj_dodavatele.py
pause
