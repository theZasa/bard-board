@echo off
rem Zugangsdaten bearbeiten und die Bots mit den neuen Daten neu starten.
rem Die Dateien liegen in %USERPROFILE%\.claudemumble (ausserhalb des Projektordners).
cd /d "%~dp0"
echo Welche Zugangsdaten moechtest du bearbeiten?
echo   [1] Mumble-Server (Adresse, Benutzer, Passwort)
echo   [2] Discord-Bot (Token)
choice /c 12 /n /m "Auswahl 1 oder 2: "
if errorlevel 2 (set "WHICH=Discord") else (set "WHICH=Mumble")
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" -Edit %WHICH%
pause
