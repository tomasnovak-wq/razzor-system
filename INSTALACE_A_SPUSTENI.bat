@echo off
title Flight Case System - Instalace
echo.
echo  ====================================================
echo   Flight Case - Vyrobni system
echo   Prvni spusteni: instalace zavislosti
echo  ====================================================
echo.

echo [1/3] Instaluji Flask (webovy server)...
pip install flask --quiet
if %errorlevel% neq 0 (
    echo CHYBA: pip install selhal. Zkuste spustit jako Administrator.
    pause
    exit /b 1
)
echo     Flask - OK

echo.
echo [2/3] Instaluji reportlab (generovani PDF faktur)...
pip install reportlab --quiet
if %errorlevel% neq 0 (
    echo CHYBA: pip install reportlab selhal.
    pause
    exit /b 1
)
echo     reportlab - OK

echo.
echo [3/3] Spoustim server...
echo.
echo  ====================================================
echo   Otevri v prohlizeci: http://localhost:5000
echo   Ze site (jine PC):   http://[IP-tohoto-PC]:5000
echo  ====================================================
echo.
echo  Server bezi. Zavreni tohoto okna = zastaveni systemu.
echo.

python app.py
pause
