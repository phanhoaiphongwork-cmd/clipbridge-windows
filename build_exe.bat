@echo off
setlocal
title Build ClipBridge.exe (Cloud)
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY (
  echo [LOI] Chua cai Python 3. Tai tai https://www.python.org/downloads/
  start "" https://www.python.org/downloads/
  goto :end
)

echo [1/3] Cai PyInstaller + qrcode + customtkinter...
%PY% -m pip install --disable-pip-version-check -q --upgrade pyinstaller qrcode customtkinter
if errorlevel 1 ( echo [LOI] pip install that bai & goto :end )

rem Bo module khong dung (giu tkinter cho giao dien) de giam dung luong (ban Cloud CAN ssl de ket noi Internet)
set EXCL=--exclude-module PIL --exclude-module png --exclude-module pillow_heif ^
 --exclude-module bz2 --exclude-module _bz2 --exclude-module lzma --exclude-module _lzma ^
 --exclude-module unittest --exclude-module pydoc ^
 --exclude-module xmlrpc --exclude-module sqlite3 --exclude-module _sqlite3 ^
 --exclude-module multiprocessing --exclude-module asyncio --exclude-module _decimal ^
 --exclude-module pyexpat --exclude-module xml --exclude-module _elementtree

set UPX=--noupx
if exist "upx.exe" set "UPX=--upx-dir ."

echo [2/3] Dong goi ClipBridge.exe...
rem App co giao dien (tkinter), khong co cua so terminal
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --optimize 2 --name ClipBridge --add-data "icon.png;." --collect-data customtkinter %EXCL% %UPX% clipbridge_app.pyw
if errorlevel 1 ( echo [LOI] PyInstaller that bai & goto :end )

echo [3/3] Don dep...
copy /y "dist\ClipBridge.exe" "ClipBridge.exe" >nul
rmdir /s /q build dist >nul 2>nul
del /q ClipBridge.spec >nul 2>nul
echo.
for %%F in (ClipBridge.exe) do echo  XONG! ClipBridge.exe = %%~zF bytes
:end
echo.
pause
