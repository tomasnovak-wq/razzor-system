@echo off
chcp 65001 >nul
cd /d "%~dp0\.."

echo.
echo === STAHNI NOVOU VERZI APLIKACE ===
echo.
echo Stahuji nejnovejsi verzi z GitHubu...
echo.

git pull

if errorlevel 1 (
    echo.
    echo CHYBA: Stazeni selhalo.
    echo Zkontroluj ze mas pristup k repozitari.
    pause
    exit /b 1
)

echo.
echo === Hotovo! ===
echo     Restartuj SPUSTIT.bat aby se nactla nova verze.
echo.
pause
