@echo off
rem SniffMe bridge runner: omara_bridge.py owns the Omara device (BLE) and
rem serves ws://127.0.0.1:8765. Rate limiting lives HERE: max 1 burst per 2s,
rem requests landing inside a closed window coalesce newest-wins (LAST mode).
cd /d "%~dp0"
if "%OMARA_BLE%"=="" set OMARA_BLE=49:3A:15:84:5E:04
rem "C:\Users\Mark\AppData\Local\Programs\Python\Python313\python.exe" omara_bridge.py --ble %OMARA_BLE% --rate 2 --rate-mode AVERAGE %*
"C:\Users\Mark\AppData\Local\Programs\Python\Python313\python.exe" omara_bridge.py --serial com4 --rate 2 --rate-mode LAST %*
