@echo off
cd /d "%~dp0"
rem Zeigt die Logs beider Bots (Zeilen beginnen mit bard-board-mumble bzw. bard-board-discord)
set "PROFILE="
if exist "%USERPROFILE%\.claudemumble\discord.env" set "PROFILE=--profile discord"
echo Strg+C beendet die Anzeige (die Bots laufen weiter).
docker compose %PROFILE% logs -f --tail 200
