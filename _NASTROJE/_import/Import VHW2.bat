@echo off
chcp 65001 >nul
cd /d "%~dp0\..\.."

echo ============================================
echo  Import profilu z VHW2.csv (novy format)
echo ============================================
echo.
echo POZOR: Tato verze importu nahrazuje stara data profilu!
echo.
echo 1. Stahni List 2 z Google Sheets jako CSV:
echo    Soubor - Stahnout - Hodnoty oddelene carkami (.csv)
echo 2. Uloz jako:  data\VHW2.csv
echo 3. Stiskni Enter pro spusteni importu.
echo.
pause
python importuj_vhw2.py
pause
