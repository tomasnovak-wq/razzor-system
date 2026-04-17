@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo  ╔══════════════════════════════════════╗
echo  ║     STÁHNOUT AKTUÁLNÍ DATA           ║
echo  ╚══════════════════════════════════════╝
echo.
echo  Stahuji databázi z cloudu...
echo  (aktuální lokální data budou přepsána)
echo.

set /p POTVRDIT="Opravdu chceš přepsat lokální data? (a/N): "
if /i not "%POTVRDIT%"=="a" (
    echo  Zrušeno.
    pause
    exit /b 0
)

if not exist "data" mkdir data

echo.
echo  Připojuji se k Fly.io...
fly sftp get /data/system.db data/system.db -a razzor-system

if errorlevel 1 (
    echo.
    echo  CHYBA: Stažení selhalo.
    echo  Zkontroluj že jsi přihlášen: fly auth login
    pause
    exit /b 1
)

echo.
echo  ╔══════════════════════════════════════╗
echo  ║   Hotovo! Data stažena lokálně.      ║
echo  ║   Spusť SPUSTIT.bat pro vývoj.       ║
echo  ╚══════════════════════════════════════╝
echo.
pause
