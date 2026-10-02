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

# Pin the current MSVC C++ runtime for every interpreter started from this .venv
$site = Join-Path $venv "Lib\site-packages"
$vcrt = Join-Path $PSScriptRoot "app\vcrt"
if((Test-Path $site) -and (Test-Path (Join-Path $vcrt "msvcp140.dll"))){
    $line = "import sys; exec(" + '"' + "try:\n import os,ctypes\n d=r'" + $vcrt.Replace('\','/') + "'\n for n in ('msvcp140.dll','msvcp140_1.dll','msvcp140_2.dll','msvcp140_atomic_wait.dll','msvcp140_codecvt_ids.dll','concrt140.dll'):\n  p=os.path.join(d,n)\n  if os.path.isfile(p): ctypes.WinDLL(p)\nexcept Exception:\n pass" + '"' + ")"
    Set-Content -Path (Join-Path $site "lf_vcrt.pth") -Value $line -Encoding ASCII
}


Write-Host "[2/4] Installing dependencies..." -ForegroundColor Yellow
& $py -m pip install --disable-pip-version-check -q --upgrade pip
& $py -m pip install --disable-pip-version-check -q --upgrade -r requirements.txt

Write-Host "[3/4] Self-test..." -ForegroundColor Yellow
& $py -c "import aiohttp; import winrt.windows.media.control; import winrt.windows.foundation; print('Core OK')"
if($LASTEXITCODE -ne 0){ exit 1 }

Write-Host "[4/4] Starting engine..." -ForegroundColor Green
# Python warnings/progress bars go to stderr; they must never abort the host.
$ErrorActionPreference = "Continue"
$env:PYTHONUNBUFFERED = "1"
& $py -X faulthandler app\server.py
$code = $LASTEXITCODE
if($code -ne 0){
    Write-Host ""
    Write-Host "Engine stopped unexpectedly (exit code $code)." -ForegroundColor Red
    $crash = Join-Path $PSScriptRoot "app\logs\crash.log"
    if(Test-Path $crash){
        Write-Host "--- app\logs\crash.log (tail) ---" -ForegroundColor DarkGray
        Get-Content $crash -Tail 25
    }
    Read-Host "Press Enter to close"
    exit $code
}
