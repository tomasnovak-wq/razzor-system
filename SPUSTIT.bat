@echo off
chcp 65001 >nul
cd /d "%~dp0"

:: Zjisti IP pres route table - nejspolehlivejsi metoda
for /f "tokens=2 delims= " %%a in ('powershell -NoProfile -Command "(Test-Connection -ComputerName (hostname) -Count 1).IPV4Address.IPAddressToString"') do set "IP=%%a"

if "%IP%"=="" (
  :: Zaloha - ipconfig
  for /f "tokens=2 delims=:" %%b in ('ipconfig ^| findstr /r "IPv4.*192\|IPv4.*10\.\|IPv4.*172\."') do (
    set "IP=%%b"
    goto :gotip
  )
)
:gotip
if defined IP set "IP=%IP: =%"

echo.
echo  ================================================
echo   Razzor Cases - Vyrobni system
echo  ================================================
echo.
echo   Tento pocitac:   http://localhost:5001
echo.
if defined IP (
  echo   Sit / tablet:    http://%IP%:5001
) else (
  echo   IP adresa: spust "ipconfig" a hledej IPv4
)
echo.
echo   Pro zastaveni: CTRL+C
echo  ================================================
echo.
python app.py 2>&1
echo.
echo Server se zastavil.
pause
