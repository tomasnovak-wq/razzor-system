@echo off
chcp 65001 >nul
echo ============================================
echo  Import dodavatelů z MATERIAL.csv
echo ============================================
echo.
echo Nejprve zastav Flask server (Ctrl+C v okně serveru).
echo Pak stiskni Enter pro spuštění importu.
pause
echo.
cd /d "%~dp0"
python importuj_dodavatele.py
