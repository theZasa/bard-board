@echo off
setlocal
rem Packt Board-Sounds, Sammlungen und Bot-Daten in eine Datei fuer den Kubernetes-Cluster.
rem Ergebnis: %USERPROFILE%\bard-board-export\bard-board-daten.tar mit Pruefsumme und ANLEITUNG.md.
rem Die Datei bleibt auf diesem PC; weitergeben z. B. per USB-Stick oder privatem Cloud-Link.
rem Zertifikat und Zugangsdaten sind nicht enthalten.
cd /d "%~dp0"

set "OUT=%USERPROFILE%\bard-board-export"
if not exist "%OUT%" mkdir "%OUT%"
if not exist "data-discord" mkdir "data-discord"

echo Packe Sounds, Sammlungen und Daten nach %OUT% ...
echo Bei rund 100.000 Dateien dauert das einige Minuten.
docker compose run --rm --no-deps -T ^
  -v "%OUT%:/export" ^
  -v "%~dp0tools:/tools:ro" ^
  -v "%~dp0data-discord:/data-discord:ro" ^
  --entrypoint python soundboard /tools/export_cluster.py /export
if errorlevel 1 (
  echo.
  echo Export fehlgeschlagen, Ausgabe siehe oben.
  pause
  exit /b 1
)
echo.
echo Fertig. Ordner wird geoeffnet.
start "" explorer "%OUT%"
pause
