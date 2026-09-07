param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "      WAISoft-Reports Installation & Setup" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "[!] Error: python is required. Please install Python 3.10+."
    exit 1
}

Write-Host "[*] Checking Python dependencies..." -ForegroundColor Gray
python -c "import requests" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[*] Installing dependencies from requirements.txt..." -ForegroundColor Gray
    python -m pip install --quiet -r requirements.txt
    Write-Host "[OK] Dependencies installed successfully." -ForegroundColor Green
} else {
    Write-Host "[OK] Dependencies are already installed." -ForegroundColor Green
}

Write-Host "[*] Launching Configuration Wizard..." -ForegroundColor Gray
python setup_wizard.py @args
