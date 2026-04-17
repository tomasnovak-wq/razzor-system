@echo off
chcp 65001 >nul
cd /d "%~dp0\.."

echo.
echo === STAHNI AKTUALNI DATA Z CLOUDU ===
echo.
echo Stahne databazi z razzor-system.fly.dev
echo (aktualni lokalni data budou PREPSAT!)
echo.

set /p POTVRDIT=Opravdu chces prepsat lokalni data? (a/N):
if /i not "%POTVRDIT%"=="a" (
    echo Zruseno.
    pause
    exit /b 0
)

if not exist "data" mkdir data

echo.
echo Stahuji databazi z cloudu...
curl -o data\system.db "https://razzor-system.fly.dev/admin/download-db?secret=razzor-upload-2026"

if errorlevel 1 (
    echo.
    echo CHYBA: Stazeni selhalo.
    pause
    exit /b 1
)

echo.
echo === Hotovo! Data stazena lokalne. ===
echo     Spust SPUSTIT.bat pro vyvoj.
echo.
pause
