# Start ChallengeTracker (no rebuild). Creates .env from example if missing.
$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example — set DISCORD_TOKEN before the bot can log in." -ForegroundColor Yellow
}

$envText = Get-Content ".env" -Raw
if ($envText -notmatch '(?m)^DISCORD_TOKEN=.+') {
    Write-Host "WARNING: DISCORD_TOKEN is empty in .env. Container may exit." -ForegroundColor Yellow
}

New-Item -ItemType Directory -Force -Path "data" | Out-Null
docker compose up -d
Write-Host "Started. Admin UI: http://127.0.0.1:8090" -ForegroundColor Green
docker compose ps
