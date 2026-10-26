# Hermes Agent for the ME3 assistant on the Windows laptop (native, no admin rights).
#
#   powershell -ExecutionPolicy Bypass -File hermes\setup.ps1              # install if needed, copy config, start
#   powershell -ExecutionPolicy Bypass -File hermes\setup.ps1 -NoInstall
#
# Mirror of setup.sh: our config.yaml and .env go into Hermes's home (%LOCALAPPDATA%\hermes),
# Hermes runs as a login Scheduled Task, the assistant talks to http://127.0.0.1:8642.
param([switch]$NoInstall)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$hermesHome = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:LOCALAPPDATA "hermes" }

if (-not (Get-Command hermes -ErrorAction SilentlyContinue) -and -not $NoInstall) {
    Write-Host "installing Hermes Agent (native Windows installer, needs Git for Windows with Bash)"
    iex (irm https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.ps1)
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + $env:Path
}
if (-not (Get-Command hermes -ErrorAction SilentlyContinue)) { throw "hermes is not on PATH; open a new terminal and rerun" }
Write-Host "hermes: $(hermes --version 2>$null | Select-Object -First 1)"

New-Item -ItemType Directory -Force -Path $hermesHome | Out-Null
Copy-Item (Join-Path $here "config.yaml") (Join-Path $hermesHome "config.yaml") -Force
$envFile = Join-Path $hermesHome ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $here "env.example") $envFile
    Write-Host "created $envFile : fill in OPENROUTER_API_KEY and API_SERVER_KEY, then rerun"
    exit 2
}
$envText = Get-Content $envFile -Raw
foreach ($k in @("OPENROUTER_API_KEY", "API_SERVER_KEY")) {
    if ($envText -notmatch "(?m)^$k=.+") { throw "$k is empty in $envFile" }
}
if ($envText -notmatch "(?m)^API_SERVER_ENABLED=true") { Add-Content $envFile "API_SERVER_ENABLED=true" }
Write-Host "config copied to $hermesHome\config.yaml"

# Gateway (API server only) as a login task; restart if it already exists.
$status = hermes gateway status 2>$null
if ($LASTEXITCODE -eq 0) { hermes gateway restart } else { hermes gateway install; hermes gateway start }
Start-Sleep -Seconds 4

$key = ($envText | Select-String -Pattern "(?m)^API_SERVER_KEY=(.+)$").Matches[0].Groups[1].Value.Trim()
$port = 8642
$m = $envText | Select-String -Pattern "(?m)^API_SERVER_PORT=(\d+)"
if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
# The gateway can take a while to come up the first time; try for about 30 s.
$r = $null; $err = $null
for ($i = 0; $i -lt 10 -and -not $r; $i++) {
    try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Headers @{ Authorization = "Bearer $key" } "http://127.0.0.1:$port/v1/models" }
    catch { $err = $_.Exception.Message; Start-Sleep -Seconds 3 }
}
if ($r) {
    Write-Host "Hermes API server answers on http://127.0.0.1:$port : $($r.Content)"
    Write-Host "now put HERMES_API_KEY=<your API_SERVER_KEY> in the assistant's .env and run: python -m assistant.main --check"
} else {
    Write-Host "API server not answering yet ($err)"
    Write-Host "check: hermes gateway status   and the logs under $hermesHome\logs"
    exit 1
}
