@echo off
rem Mo app ClipBridge (co giao dien, khong can cua so terminal)
cd /d "%~dp0"
where pyw >nul 2>nul && ( start "" pyw -3 clipbridge_app.pyw & exit /b 0 )
where pythonw >nul 2>nul && ( start "" pythonw clipbridge_app.pyw & exit /b 0 )
echo [LOI] Chua cai Python 3. Tai tai https://www.python.org/downloads/ (tick "Add python.exe to PATH")
start "" https://www.python.org/downloads/
pause
