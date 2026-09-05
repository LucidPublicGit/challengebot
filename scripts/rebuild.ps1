# Rebuild image + recreate container after code updates. DB volume ./data is kept.
#   .\scripts\rebuild.ps1           # cached build (usual after code change)
#   .\scripts\rebuild.ps1 -NoCache  # full rebuild
#   .\scripts\rebuild.ps1 -Logs     # tail logs after up
param(
    [switch]$NoCache,
    [switch]$Logs
)

$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example — set DISCORD_TOKEN." -ForegroundColor Yellow
}

New-Item -ItemType Directory -Force -Path "data" | Out-Null

Write-Host "=== ChallengeTracker rebuild ===" -ForegroundColor Cyan
if ($NoCache) {
    docker compose build --no-cache
} else {
    docker compose build
}
docker compose up -d --force-recreate
Write-Host "Rebuilt. Admin UI: http://127.0.0.1:8090" -ForegroundColor Green
docker compose ps

if ($Logs) {
    docker compose logs -f --tail=80
}
