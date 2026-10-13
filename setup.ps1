#Requires -Version 5.1
<#
  Richtet das Soundboard (Mumble und optional Discord) ein und startet es.
  - prueft/installiert Docker Desktop
  - legt die Zugangsdaten-Dateien AUSSERHALB des Projektordners an
  - ergaenzt neue Einstellungen in .env
  - baut und startet die Container und oeffnet das Webinterface
  Kann beliebig oft ausgefuehrt werden (auch zum Aktualisieren nach Code-Aenderungen).
  -Edit Mumble|Discord oeffnet vorher die jeweilige Zugangsdaten-Datei im Editor (zugangsdaten.bat).
#>
param(
    [ValidateSet('', 'Mumble', 'Discord')]
    [string]$Edit = ''
)
$ProgressPreference = 'SilentlyContinue'
Set-Location -LiteralPath $PSScriptRoot

function Info([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Ok([string]$Message) { Write-Host "OK  $Message" -ForegroundColor Green }
function Warn([string]$Message) { Write-Host "!!  $Message" -ForegroundColor Yellow }
function Fail([string]$Message) { Write-Host "XX  $Message" -ForegroundColor Red; exit 1 }

# ------------------------------------------------------------------ Docker Desktop
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Warn 'Docker Desktop ist nicht installiert.'
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        $answer = Read-Host 'Jetzt mit winget installieren? (j/n)'
        if ($answer -match '^[jJyY]') {
            winget install --exact --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -ne 0) { Fail 'Installation von Docker Desktop fehlgeschlagen.' }
            Warn 'Docker Desktop ist installiert. Bitte Windows neu starten (WSL 2 wird eingerichtet),'
            Warn 'Docker Desktop einmal oeffnen und die Bedingungen bestaetigen, dann start.bat erneut ausfuehren.'
            exit 0
        }
    }
    Fail 'Bitte Docker Desktop installieren: https://www.docker.com/products/docker-desktop/'
}

function Test-DockerEngine {
    docker info --format '{{.ServerVersion}}' *> $null
    return ($LASTEXITCODE -eq 0)
}

if (-not (Test-DockerEngine)) {
    $desktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
    if (Test-Path -LiteralPath $desktop) {
        Info 'Starte Docker Desktop ...'
        Start-Process -FilePath $desktop | Out-Null
    }
    $deadline = (Get-Date).AddMinutes(3)
    while (-not (Test-DockerEngine)) {
        if ((Get-Date) -gt $deadline) { Fail 'Docker laeuft nicht. Bitte Docker Desktop starten und erneut versuchen.' }
        Start-Sleep -Seconds 3
    }
}
docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Fail 'docker compose fehlt. Bitte Docker Desktop aktualisieren.' }
Ok 'Docker laeuft'

# ------------------------------------------------------------------ Zugangsdaten
$secretDir = Join-Path $env:USERPROFILE '.claudemumble'
$secretFile = Join-Path $secretDir 'zugang.env'
if (-not (Test-Path -LiteralPath $secretFile)) {
    New-Item -ItemType Directory -Force -Path $secretDir | Out-Null
    $template = @(
        '# Zugangsdaten fuer den Mumble-Server.',
        '# Diese Datei liegt absichtlich ausserhalb des Projektordners.',
        '# Werte mit Sonderzeichen ($ # Leerzeichen) in einfache Anfuehrungszeichen setzen:',
        "#   MUMBLE_PASSWORD='geh`$im#123'",
        '',
        'MUMBLE_HOST=',
        'MUMBLE_PORT=64738',
        'MUMBLE_USER=Soundboard',
        'MUMBLE_PASSWORD=',
        '',
        '# Optional: Zugangstoken fuer geschuetzte Kanaele (kommagetrennt)',
        'MUMBLE_TOKENS='
    )
    Set-Content -LiteralPath $secretFile -Value $template -Encoding ASCII
    Info "Zugangsdaten-Datei angelegt: $secretFile"
}

function Test-Secrets {
    return [bool](Select-String -LiteralPath $secretFile -Pattern '^\s*MUMBLE_HOST\s*=\s*\S' -Quiet)
}

if ($Edit -eq 'Mumble' -or -not (Test-Secrets)) {
    Info 'Bitte im Editor Server-Adresse, Benutzername und ggf. Passwort eintragen, speichern und den Editor schliessen.'
    Start-Process -FilePath notepad.exe -ArgumentList "`"$secretFile`"" -Wait
    if (-not (Test-Secrets)) { Fail "MUMBLE_HOST ist noch leer. Datei: $secretFile" }
}
Ok 'Mumble-Zugangsdaten vorhanden (Inhalt wird nicht angezeigt)'

# Discord ist optional: der Bot startet erst, wenn in discord.env ein Token steht.
$discordFile = Join-Path $secretDir 'discord.env'
if (-not (Test-Path -LiteralPath $discordFile)) {
    $template = @(
        '# Token des Discord-Bots (discord.com/developers > deine Anwendung > Bot > Reset Token).',
        '# Diese Datei liegt absichtlich ausserhalb des Projektordners.',
        '# Leer lassen, wenn nur der Mumble-Bot laufen soll.',
        '',
        'DISCORD_TOKEN='
    )
    Set-Content -LiteralPath $discordFile -Value $template -Encoding ASCII
}

function Test-DiscordToken {
    return [bool](Select-String -LiteralPath $discordFile -Pattern '^\s*DISCORD_TOKEN\s*=\s*\S' -Quiet)
}

if ($Edit -eq 'Discord') {
    Info 'Bitte im Editor den Bot-Token hinter DISCORD_TOKEN= einfuegen, speichern und den Editor schliessen.'
    Start-Process -FilePath notepad.exe -ArgumentList "`"$discordFile`"" -Wait
}
$discord = Test-DiscordToken
if ($discord) { Ok 'Discord-Token vorhanden (Inhalt wird nicht angezeigt)' }
else { Info 'Kein Discord-Token - nur der Mumble-Bot startet (Discord einrichten: zugangsdaten.bat)' }

# ------------------------------------------------------------------ Projekt
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
    Info '.env aus .env.example erstellt (Einstellungen ohne Zugangsdaten)'
} else {
    # Einstellungen, die neuere Versionen mitbringen, mit ihren Kommentaren hinten anfuegen.
    # Vorhandene Zeilen (auch auskommentierte) bleiben unangetastet.
    $have = @{}
    foreach ($line in Get-Content -LiteralPath '.env') {
        if ($line -match '^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=') { $have[$Matches[1]] = $true }
    }
    $block = @()
    $added = @()
    $comments = @()
    foreach ($line in Get-Content -LiteralPath '.env.example') {
        if ($line -match '^\s*([A-Z][A-Z0-9_]*)\s*=') {
            if (-not $have.ContainsKey($Matches[1])) {
                $block += $comments
                $block += $line
                $added += $Matches[1]
            }
            $comments = @()
        } elseif ($line -match '^\s*#') {
            $comments += $line
        } else {
            $comments = @()
        }
    }
    if ($added.Count -gt 0) {
        Add-Content -LiteralPath '.env' -Encoding ASCII -Value (@('', "# --- Ergaenzt am $(Get-Date -Format 'yyyy-MM-dd') ---") + $block)
        Info "Neue Einstellungen in .env ergaenzt: $($added -join ', ')"
    }
}
New-Item -ItemType Directory -Force -Path 'sounds', 'data', 'data-discord' | Out-Null

function Get-Setting([string]$Name, [int]$Default) {
    $match = Select-String -LiteralPath '.env' -Pattern "^\s*$Name\s*=\s*(\d+)" | Select-Object -First 1
    if ($match) { return [int]$match.Matches[0].Groups[1].Value }
    return $Default
}
$port = Get-Setting 'HOST_PORT' 8080
$discordPort = Get-Setting 'DISCORD_HOST_PORT' 8081

$composeArgs = @()
if ($discord) { $composeArgs = @('--profile', 'discord') }
else {
    # Token entfernt: einen noch laufenden Discord-Bot beenden.
    docker compose --profile discord rm --stop --force discord *> $null
}

# Renamed from claudemumble: remove the old containers, otherwise their restart policy
# brings them back and they hold the ports. Data lives in bind mounts and is kept.
$oldContainers = @(docker ps -aq --filter 'name=^claudemumble(-discord)?$')
if ($oldContainers.Count -gt 0) {
    Info 'Entferne die alten Container "claudemumble" (Daten bleiben erhalten) ...'
    docker rm --force $oldContainers *> $null
    docker network rm claudemumble_default *> $null
}

# Version for the web interface and /help: commit and its date, "+" for uncommitted changes.
# The commit date (not today) keeps the build cached when nothing changed.
$env:BARD_BOARD_REVISION = ''
$env:BARD_BOARD_DATE = ''
if (Get-Command git -ErrorAction SilentlyContinue) {
    $revision = git rev-parse HEAD 2> $null
    if ($LASTEXITCODE -eq 0 -and $revision) {
        $dirty = git status --porcelain --untracked-files=no 2> $null
        $env:BARD_BOARD_REVISION = $revision + $(if ($dirty) { '+' } else { '' })
        $env:BARD_BOARD_DATE = git log -1 --format=%cs 2> $null
    }
}

Info 'Baue und starte die Container (beim ersten Mal dauert das ein paar Minuten) ...'
docker compose @composeArgs up -d --build --remove-orphans
if ($LASTEXITCODE -ne 0) { Fail 'docker compose up ist fehlgeschlagen (Ausgabe oben).' }

function Wait-Web([string]$Url, [string]$Service) {
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        try {
            Invoke-WebRequest -Uri "$Url/healthz" -UseBasicParsing -TimeoutSec 3 | Out-Null
            return
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    docker compose @composeArgs logs --tail 40 $Service
    Fail "Das Webinterface ($Url) antwortet nicht. Log siehe oben."
}

$url = "http://localhost:$port"
Wait-Web $url 'soundboard'
Ok "Mumble-Soundboard laeuft: $url"
if ($discord) {
    Wait-Web "http://localhost:$discordPort" 'discord'
    Ok "Discord-Soundboard laeuft: http://localhost:$discordPort (Umschalter oben links im Webinterface)"
}
Start-Process $(if ($Edit -eq 'Discord' -and $discord) { "http://localhost:$discordPort" } else { $url })
