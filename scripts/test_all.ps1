<#
.SYNOPSIS
    One-click test runner for E-Commerce Microservices on Windows PowerShell.
#>
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$rootDir = Split-Path -Parent $scriptDir
Set-Location $rootDir

$env:GATEWAY_URL = if ($env:GATEWAY_URL) { $env:GATEWAY_URL } else { "http://127.0.0.1:8000" }
$env:ADMIN_EMAIL = if ($env:ADMIN_EMAIL) { $env:ADMIN_EMAIL } else { "admin@example.com" }
$env:ADMIN_PASSWORD = if ($env:ADMIN_PASSWORD) { $env:ADMIN_PASSWORD } else { "AdminPassword123!" }

Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host "          E-COMMERCE MICROSERVICES ALL-IN-ONE TEST RUNNER             " -ForegroundColor Cyan
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host "Target Gateway: $env:GATEWAY_URL"
Write-Host "Working Dir:    $rootDir`n"

# 1. Check Docker status
try {
    docker info > $null 2>&1
} catch {
    Write-Error "Docker Desktop is not running or accessible. Please start Docker Desktop."
    exit 1
}

# 2. Check running containers
$running = (docker compose ps --status running -q 2>$null | Measure-Object -Line).Lines
if ($running -lt 11) {
    Write-Host "Starting / updating Docker Compose containers..." -ForegroundColor Yellow
    docker compose up -d
} else {
    Write-Host "All containers already running (Count: $running)." -ForegroundColor Green
}

# 3. Wait for gateway
Write-Host -NoNewline "Waiting for API Gateway at $env:GATEWAY_URL/health ..."
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $res = Invoke-RestMethod -Uri "$env:GATEWAY_URL/health" -TimeoutSec 2 -ErrorAction SilentlyContinue
        if ($res.status -eq "UP") {
            $ready = $true
            break
        }
    } catch {}
    Write-Host -NoNewline "."
    Start-Sleep -Seconds 2
}

if (-not $ready) {
    Write-Host " FAILED." -ForegroundColor Red
    docker compose ps
    exit 1
}
Write-Host " READY!" -ForegroundColor Green

# 4. Display container status
Write-Host "`n--- Container Status ---" -ForegroundColor Cyan
docker compose ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# 5. Auto-bootstrap administrator & initial catalog if needed
Write-Host "`nVerifying / bootstrapping baseline test data..." -ForegroundColor Cyan
python scripts/bootstrap.py

# 6. Run test suite
Write-Host "`nRunning automated end-to-end integration test suite..." -ForegroundColor Cyan
python scripts/test_microservices_e2e.py
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "`n======================================================================" -ForegroundColor Green
    Write-Host "           ALL MICROSERVICES FEATURES VERIFIED SUCCESSFULLY!         " -ForegroundColor Green
    Write-Host "======================================================================" -ForegroundColor Green
} else {
    Write-Host "`n======================================================================" -ForegroundColor Red
    Write-Host "            SOME TESTS FAILED (EXIT CODE: $exitCode)          " -ForegroundColor Red
    Write-Host "======================================================================" -ForegroundColor Red
}

exit $exitCode
