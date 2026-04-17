@echo off
chcp 65001 >nul
echo ============================================
echo  Stahuji změny z Google Drive
echo ============================================
echo.

set LOCAL=%~dp0
set GDRIVE=G:\Můj disk\Claude\flightcase-system

echo Z: %GDRIVE%
echo Na: %LOCAL%
echo.

if not exist "%GDRIVE%" (
  echo CHYBA: Google Drive složka nenalezena: %GDRIVE%
  echo Zkontroluj připojení Google Drive.
  pause
  exit /b 1
)

robocopy "%GDRIVE%" "%LOCAL%" ^
  *.py *.html *.bat ^
  /XD __pycache__ data ^
  /XF *.db *.pyc ^
  /NFL /NDL /NJH /NJS

echo.
echo Hotovo! Máš nejnovější verzi od kolegy.
echo Restartuj server (SPUSTIT.bat).
pause
