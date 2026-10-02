$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host ""
Write-Host "==============================================" -ForegroundColor Magenta
Write-Host "   LYRIC FESTIVAL // BUILD LyricFestival.exe  " -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Magenta
Write-Host ""

function Find-Python {
    foreach($c in @(@("py","-3.12"),@("py","-3.11"),@("py","-3"),@("python",""))){
        try{
            if($c[1] -eq ""){ & $c[0] --version *> $null } else { & $c[0] $c[1] --version *> $null }
            if($LASTEXITCODE -eq 0){ return $c }
        }catch{}
    }
    return $null
}

$venv = Join-Path $root ".venv"
$py = Join-Path $venv "Scripts\python.exe"
if(-not (Test-Path $py)){
    $python = Find-Python
    if($null -eq $python){
        Write-Host "Python 3.11/3.12 not found. Install it from python.org (tick 'Add to PATH') and run again." -ForegroundColor Red
        exit 1
    }
    Write-Host "[1/4] Creating local environment..." -ForegroundColor Yellow
    if($python[1] -eq ""){ & $python[0] -m venv $venv } else { & $python[0] $python[1] -m venv $venv }
    if(-not (Test-Path $py)){ Write-Host "Could not create .venv" -ForegroundColor Red; exit 1 }
}else{
    Write-Host "[1/4] Existing environment found." -ForegroundColor Yellow
}


# Pin the current MSVC C++ runtime for every interpreter started from this .venv
$site = Join-Path $venv "Lib\site-packages"
$vcrt = Join-Path $root "app\vcrt"
if((Test-Path $site) -and (Test-Path (Join-Path $vcrt "msvcp140.dll"))){
    $line = "import sys; exec(" + '"' + "try:\n import os,ctypes\n d=r'" + $vcrt.Replace('\','/') + "'\n for n in ('msvcp140.dll','msvcp140_1.dll','msvcp140_2.dll','msvcp140_atomic_wait.dll','msvcp140_codecvt_ids.dll','concrt140.dll'):\n  p=os.path.join(d,n)\n  if os.path.isfile(p): ctypes.WinDLL(p)\nexcept Exception:\n pass" + '"' + ")"
    Set-Content -Path (Join-Path $site "lf_vcrt.pth") -Value $line -Encoding ASCII
}

Write-Host "[2/4] Installing dependencies (first time takes a few minutes)..." -ForegroundColor Yellow
& $py -m pip install --disable-pip-version-check -q --upgrade pip
& $py -m pip install --disable-pip-version-check -q -r requirements.txt "pywebview>=5.3,<7" "pyinstaller>=6.10,<7"
if($LASTEXITCODE -ne 0){ Write-Host "Dependency install failed." -ForegroundColor Red; exit 1 }

Write-Host "[3/4] Packaging (2-5 minutes)..." -ForegroundColor Yellow
$work = Join-Path $PSScriptRoot "work"
$dist = Join-Path $PSScriptRoot "dist"
& $py -m PyInstaller --noconfirm --clean --log-level WARN --distpath $dist --workpath $work (Join-Path $PSScriptRoot "LyricFestival.spec")
if($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $dist "LyricFestival.exe"))){
    Write-Host "Build failed." -ForegroundColor Red; exit 1
}

Write-Host "[4/4] Finishing..." -ForegroundColor Yellow
Copy-Item (Join-Path $dist "LyricFestival.exe") (Join-Path $root "LyricFestival.exe") -Force
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $dist -Recurse -Force -ErrorAction SilentlyContinue

$exe = Join-Path $root "LyricFestival.exe"
$mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host ""
Write-Host "DONE -> $exe ($mb MB)" -ForegroundColor Green
Write-Host "The .exe is self-contained: you can copy it anywhere." -ForegroundColor Green
exit 0
