# Laptop side: control Donna on the Pi over ssh (Host "donna" in ~/.ssh/config).
#   donna status | start | stop | restart | logs | ui
# Install once:  powershell -ExecutionPolicy Bypass -File tools\donna.ps1 install
#   (adds a `donna` function to your PowerShell profile; open a new terminal afterwards)
param([string]$cmd = "status")

$ssh = @("-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=8", "donna")
$pi = '~/.local/bin/donna'

switch ($cmd) {
    "status"  { ssh @ssh "$pi status" }
    "start"   { ssh @ssh "$pi start" }
    "stop"    { ssh @ssh "$pi stop" }
    "restart" { ssh @ssh "$pi restart" }
    "logs"    { ssh -t @ssh "$pi logs" }
    "ui" {
        # tunnel + browser; the tunnel stays open in this window (Ctrl+C closes it, Donna keeps running)
        Start-Process "http://localhost:18780/?ws=18765"
        ssh -N donna
    }
    "install" {
        $line = "function donna { & '$PSCommandPath' @args }"
        if (-not (Test-Path $PROFILE)) { New-Item -ItemType File -Force $PROFILE | Out-Null }
        if (-not (Select-String -Path $PROFILE -SimpleMatch "donna.ps1" -Quiet)) {
            Add-Content -Path $PROFILE -Value $line -Encoding utf8
        }
        Write-Host "Installed. Open a new PowerShell window and type: donna status"
    }
    default { Write-Host "usage: donna status|start|stop|restart|logs|ui|install" }
}
