# Creates "Voice Assistant.lnk" (with the logo icon) in this folder and on the Desktop.
# Run once:  powershell -ExecutionPolicy Bypass -File app\make_shortcut.ps1
$app   = Split-Path -Parent $MyInvocation.MyCommand.Path
$root  = Split-Path -Parent $app
$bat   = Join-Path $app "Voice Assistant.bat"
$icon  = Join-Path $app "assets\logo.ico"
$shell = New-Object -ComObject WScript.Shell

foreach ($dir in @($root, [Environment]::GetFolderPath("Desktop"))) {
  $lnk = $shell.CreateShortcut((Join-Path $dir "Voice Assistant.lnk"))
  $lnk.TargetPath       = $bat
  $lnk.WorkingDirectory = $root
  $lnk.IconLocation     = "$icon,0"
  $lnk.Description      = "ME3 full-duplex voice assistant"
  $lnk.WindowStyle      = 7   # minimized console
  $lnk.Save()
  Write-Host "created $(Join-Path $dir 'Voice Assistant.lnk')"
}
