@echo off
cd /d "%~dp0\.."

echo.
echo === NASADIT NA CLOUD ===
echo.

set /p ZPRAVA=Popis zmeny (Enter = aktualizace):
if "%ZPRAVA%"=="" set ZPRAVA=aktualizace

set /p AUTOR=Tvoje jmeno (Enter = Tomas):
if "%AUTOR%"=="" set AUTOR=Tomas

echo.
echo [1/4] Zapisuji verzi...
python update_version.py "%ZPRAVA%" "%AUTOR%"

echo.
echo [2/4] Ukladam do Gitu...
git add -A
git commit -m "%ZPRAVA%"

echo.
echo [3/4] Nahravam na GitHub...
git push
if errorlevel 1 goto chyba

echo.
echo [4/4] Nasazuji na Fly.io...
fly deploy
if errorlevel 1 goto chyba

echo.
echo === Hotovo! https://razzor-system.fly.dev ===
echo.
echo  Tip: Pokud jsi pridal novou funkci nebo zmenil architekturu,
echo  rekni Claudovi: "Aktualizuj CLAUDE.md"
echo.
pause
exit /b 0

:chyba
echo.
echo CHYBA - zkontroluj vystup vys
pause
exit /b 1
