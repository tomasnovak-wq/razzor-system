@echo off
chcp 65001 >nul
echo ============================================
echo  Import profilu z VHW.csv
echo ============================================
echo.
echo Spust nejprve MIGRACE.bat pokud jsi tak jeste neucinil.
echo Server muze bezet.
echo.
cd /d "%~dp0"
python importuj_vhw_profily.py
