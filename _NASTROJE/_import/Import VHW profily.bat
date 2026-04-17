@echo off
chcp 65001 >nul
cd /d "%~dp0\..\.."

echo ============================================
echo  Import profilu z VHW.csv
echo ============================================
echo.
echo Server muze bezet.
echo.
python importuj_vhw_profily.py
pause
