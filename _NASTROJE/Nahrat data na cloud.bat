@echo off
chcp 65001 >nul
cd /d "%~dp0\.."

echo.
echo === NAHRAT LOKALNI DATA NA CLOUD ===
echo.
echo Nahraje tvou lokalni databazi na razzor-system.fly.dev
echo (cloudova data budou PREPSAT!)
echo.

set /p POTVRDIT=Opravdu chces prepsat cloudova data? (a/N):
if /i not "%POTVRDIT%"=="a" (
    echo Zruseno.
    pause
    exit /b 0
)

echo.
echo Nahravam databazi...
curl -s -X POST https://razzor-system.fly.dev/admin/upload-db ^
  -H "X-Upload-Secret: razzor-upload-2026" ^
  -F "file=@data/system.db"

echo.
echo === Hotovo! Data jsou na cloudu. ===
echo.
pause
