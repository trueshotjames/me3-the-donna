# Runs every scenario JSON in a folder through benchmark/audio_replay.py (real pipeline, synthesized speech in).
# Usage: powershell -ExecutionPolicy Bypass -File run_scenarios.ps1 -Project <ME3 folder> -Scenarios <folder> -Out <folder> [-Pause 20]
param(
    [Parameter(Mandatory = $true)][string]$Project,
    [Parameter(Mandatory = $true)][string]$Scenarios,
    [Parameter(Mandatory = $true)][string]$Out,
    [int]$Pause = 20
)
$ErrorActionPreference = "Continue"
$env:PYTHONIOENCODING = "utf-8"
New-Item -ItemType Directory -Force $Out | Out-Null
$py = Join-Path $Project ".venv\Scripts\python.exe"
$files = Get-ChildItem $Scenarios -Filter *.json | Sort-Object Name
$first = $true
foreach ($f in $files) {
    if (-not $first) { Start-Sleep -Seconds $Pause }
    $first = $false
    Write-Output "=== $($f.Name) $(Get-Date -Format HH:mm:ss) ==="
    Push-Location $Project
    & $py -m benchmark.audio_replay $f.FullName --out $Out *> (Join-Path $Out "$($f.BaseName).stdout.log")
    Pop-Location
    Write-Output "    exit $LASTEXITCODE"
}
Write-Output "done $(Get-Date -Format HH:mm:ss)"
