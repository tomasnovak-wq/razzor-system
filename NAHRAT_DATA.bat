@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo  ╔══════════════════════════════════════╗
echo  ║     NAHRÁT LOKÁLNÍ DATA NA CLOUD     ║
echo  ╚══════════════════════════════════════╝
echo.
echo  Nahraje tvou lokální databázi na razzor-system.fly.dev
echo  (cloudová data budou přepsána!)
echo.

set /p POTVRDIT="Opravdu chceš přepsat cloudová data? (a/N): "
if /i not "%POTVRDIT%"=="a" (
    echo  Zrušeno.
    pause
    exit /b 0
)

echo.
echo  Nahrávám databázi...
curl -s -X POST https://razzor-system.fly.dev/admin/upload-db ^
  -H "X-Upload-Secret: razzor-upload-2026" ^
  -F "file=@data/system.db"

echo.
echo  ╔══════════════════════════════════════╗
echo  ║   Hotovo! Data jsou na cloudu.       ║
echo  ╚══════════════════════════════════════╝
echo.
pause
