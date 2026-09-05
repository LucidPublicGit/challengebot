$ErrorActionPreference = "Continue"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))
Write-Host "=== compose ===" -ForegroundColor Cyan
docker compose ps
Write-Host "`n=== health ===" -ForegroundColor Cyan
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8090/health" -UseBasicParsing -TimeoutSec 3
    Write-Host "http://127.0.0.1:8090/health -> $($r.StatusCode) $($r.Content)"
} catch {
    Write-Host "Admin UI not reachable: $($_.Exception.Message)" -ForegroundColor Yellow
}
Write-Host "`n=== last logs ===" -ForegroundColor Cyan
docker compose logs --tail=30 2>&1
