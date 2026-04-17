@echo off
chcp 65001 >nul
echo ============================================
echo  Nahrávám změny na Google Drive
echo ============================================
echo.

set LOCAL=%~dp0
set GDRIVE=G:\Můj disk\Claude\flightcase-system

echo Z: %LOCAL%
echo Na: %GDRIVE%
echo.

robocopy "%LOCAL%" "%GDRIVE%" ^
  app.py app.html database.py ^
  importuj_vhw2.py importuj_vhw_profily.py importuj_dodavatele.py ^
  migrace.py migrace_auto.py pdf_faktura.py ^
  *.bat *.py ^
  /XD __pycache__ data ^
  /XF *.db *.pyc ^
  /MIR /NFL /NDL /NJH /NJS

echo.
echo Hotovo! Kolega vidí změny na Google Drive.
pause
