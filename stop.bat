@echo off
cd /d "%~dp0"
rem Stoppt beide Bots (Mumble und, falls eingerichtet, Discord)
set "PROFILE="
if exist "%USERPROFILE%\.claudemumble\discord.env" set "PROFILE=--profile discord"
docker compose %PROFILE% down
pause
