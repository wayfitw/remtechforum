@echo off
REM Kiosk launcher for the webcam test. Listens on all interfaces, so the page
REM is reachable both from this machine and from a phone in the same network
REM (required for the QR code and phone upload to work).
setlocal
cd /d "%~dp0backend"

if not exist ".venv\Scripts\python.exe" (
  echo [!] No environment found. Run run.bat first, it installs everything.
  pause
  exit /b 1
)

for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4" ^| findstr /v "127."') do (
  if not defined LAN set LAN=%%a
)
set LAN=%LAN: =%

echo.
echo   Kiosk on this PC :  http://localhost:8010
echo   From a phone     :  http://%LAN%:8010
echo.
echo   Stop with Ctrl+C in this window.
echo.

".venv\Scripts\python.exe" -m uvicorn main:app --host 0.0.0.0 --port 8010
endlocal
