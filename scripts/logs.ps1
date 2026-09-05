param([int]$Tail = 100)
$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))
docker compose logs -f --tail=$Tail
