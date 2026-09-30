$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host ""
Write-Host "====================================================" -ForegroundColor Magenta
Write-Host "   LYRIC FESTIVAL REWRITE // MEDIA ORACLE ENGINE   " -ForegroundColor Cyan
Write-Host "====================================================" -ForegroundColor Magenta
Write-Host ""

function Find-Python {
    $candidates = @(
        @("py","-3.12"),
        @("py","-3.11"),
        @("py","-3"),
        @("python","")
    )
    foreach($c in $candidates){
        try{
            if($c[1] -eq ""){ & $c[0] --version *> $null }
            else{ & $c[0] $c[1] --version *> $null }
            if($LASTEXITCODE -eq 0){ return $c }
        }catch{}
    }
    return $null
}

$python=Find-Python
if($null -eq $python){
    Write-Host "Python 3 not found." -ForegroundColor Red
    exit 1
}

$venv=Join-Path $PSScriptRoot ".venv"
if(-not (Test-Path $venv)){
    Write-Host "[1/4] Creating local environment..." -ForegroundColor Yellow
    if($python[1] -eq ""){ & $python[0] -m venv $venv }
    else{ & $python[0] $python[1] -m venv $venv }
}else{
    Write-Host "[1/4] Existing environment found." -ForegroundColor Yellow
}

$py=Join-Path $venv "Scripts\python.exe"

Write-Host "[2/4] Installing dependencies..." -ForegroundColor Yellow
& $py -m pip install --disable-pip-version-check -q --upgrade pip
& $py -m pip install --disable-pip-version-check -q --upgrade -r requirements.txt

Write-Host "[3/4] Self-test..." -ForegroundColor Yellow
& $py -c "import aiohttp; import winrt.windows.media.control; import winrt.windows.foundation; print('Core OK')"
if($LASTEXITCODE -ne 0){ exit 1 }

Write-Host "[4/4] Starting engine..." -ForegroundColor Green
& $py app\server.py
