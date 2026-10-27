param([string]$Distro='Codex-RF-Verify-20261019')
$ErrorActionPreference='Stop'
$taskRoot=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$taskOut=Join-Path $taskRoot 'results/hrrn-native-stage4-20261026'
$taskGateway=((& wsl.exe -d $Distro -- ip -4 route show default) -split '\s+')[2]
$taskGuest=((& wsl.exe -d $Distro -- hostname -i).Trim() -split '\s+')[0]
if (-not $taskGateway -or -not $taskGuest) {throw 'Cannot find local WSL transport addresses'}
# This helper owns only the process it starts; never kill an existing service.
$taskProcess=Start-Process -FilePath (Join-Path $taskRoot '.venv/Scripts/python.exe') -ArgumentList @('scripts/hrrn_rf_server.py','--bridge-host',$taskGateway,'--allowed-client',$taskGuest) -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskOut 'rf-service.jsonl') -RedirectStandardError (Join-Path $taskOut 'rf-service-error.log') -PassThru
try {
 $taskReady=$false
 for ($taskAttempt=0;$taskAttempt -lt 30;$taskAttempt++) {
  if ($taskProcess.HasExited) {throw 'Test RF service exited; inspect rf-service-error.log (possibly port already in use)'}
  try {$taskHealth=Invoke-RestMethod 'http://127.0.0.1:18084/health';if ($taskHealth.model_ready) {$taskReady=$true;break}} catch {}
  Start-Sleep -Seconds 1
 }
 if (-not $taskReady) {throw 'Frozen RF service not ready'}
 & (Join-Path $PSScriptRoot 'test_hrrn_native.ps1') -Distro $Distro -RFEndpoint "http://${taskGateway}:18083/v1/rank"
 & (Join-Path $PSScriptRoot 'build_hrrn_native.ps1')
} finally {
 if (-not $taskProcess.HasExited) {Stop-Process -Id $taskProcess.Id}
}
