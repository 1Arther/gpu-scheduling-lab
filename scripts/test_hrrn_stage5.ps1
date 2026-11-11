param([int]$PVCRounds=20,[string]$Attempt='initial')
$ErrorActionPreference='Stop'
if ($Attempt -notmatch '^[a-z0-9-]+$') {throw 'Use a simple attempt ID'}
$taskRoot=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$taskOut=Join-Path $taskRoot "results/hrrn-k8s-stage5-20261027/local/$Attempt"
if (Test-Path $taskOut) {throw 'Preserve previous attempt; use a new attempt ID'}
New-Item -ItemType Directory $taskOut | Out-Null
$taskGo=Join-Path $taskRoot '.tools/go1.26.3/go/bin/go.exe'
$taskOld=Join-Path $taskRoot 'results/hrrn-native-stage4-20261026'
$taskSource=Join-Path $taskRoot 'scheduler/volcano-rf'
$env:GOMODCACHE=Join-Path $taskRoot '.tools/gomodcache';$env:GOCACHE=Join-Path $taskRoot '.tools/gocache'
$env:GOPATH=Join-Path $taskRoot '.tools/gopath';$env:GOTOOLCHAIN='local';$env:GOMAXPROCS='2'
$env:GOOS='linux';$env:GOARCH='amd64';$env:CGO_ENABLED='0'
$taskLinuxRoot='/mnt/c'+$taskRoot.Substring(2).Replace('\','/')
$taskLinuxOut="$taskLinuxRoot/results/hrrn-k8s-stage5-20261027/local/$Attempt"
$taskGateway=((& wsl.exe -d Codex-RF-Verify-20261019 -- ip -4 route show default) -split '\s+')[2]
$taskGuestAddress=(& wsl.exe -d Codex-RF-Verify-20261019 -- ip -4 -o addr show eth0) -join ' '
$taskGuest=if ($taskGuestAddress -match '\binet\s+(\d+\.\d+\.\d+\.\d+)/') {$Matches[1]} else {$null}
if (-not $taskGateway -or -not $taskGuest) {throw 'Local WSL address lookup failed'}
$taskProcess=Start-Process -FilePath (Join-Path $taskRoot '.venv/Scripts/python.exe') -ArgumentList @('scripts/hrrn_rf_server.py','--bridge-host',$taskGateway,'--allowed-client',$taskGuest) -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskOut 'rf-service.jsonl') -RedirectStandardError (Join-Path $taskOut 'rf-service-error.log') -PassThru
$taskCoreExit=$null
Push-Location $taskSource
try {
 for ($taskWait=0;$taskWait -lt 30;$taskWait++) {
  if ($taskProcess.HasExited) {throw 'Local frozen RF service exited'}
  try {$taskHealth=Invoke-RestMethod 'http://127.0.0.1:18084/health';if ($taskHealth.model_ready) {break}} catch {}
  Start-Sleep -Seconds 1
 }
 if (-not $taskHealth.model_ready) {throw 'Local RF not ready'}
 foreach ($taskPkg in @('plugins/hrrn','actions/allocate')) {
  $taskName=$taskPkg.Replace('/','-')
  & $taskGo test -p 2 -c -o (Join-Path $taskOut "$taskName.test") "./pkg/scheduler/$taskPkg" > (Join-Path $taskOut "$taskName-build.log") 2>&1
  if ($LASTEXITCODE -ne 0) {throw "Build failed: $taskPkg"}
 }
 & $taskGo tool test2json -t -p hrrn-stage5 wsl.exe -d Codex-RF-Verify-20261019 --exec env "RF_LIVE_ENDPOINT=http://${taskGateway}:18083/v1/rank" "HRRN_STAGE5_RESULTS=$taskLinuxOut" "HRRN_RESULTS=$taskLinuxOut" "HRRN_MANIFESTS=$taskLinuxRoot/k8s/hrrn-stage4" "$taskLinuxOut/plugins-hrrn.test" '-test.v' '-test.count=1' '-test.timeout=5m' > (Join-Path $taskOut 'hrrn-tests.jsonl')
 $taskCoreExit=$LASTEXITCODE
 $taskEvents=Get-Content (Join-Path $taskOut 'hrrn-tests.jsonl') | ForEach-Object {$_ | ConvertFrom-Json}
 $taskSkipped=@($taskEvents | Where-Object {$_.Action -eq 'skip'})
 if ($taskSkipped.Count) {throw 'Required native/RF tests were skipped; do not report a passing live-RF validation'}
 $taskBinaries=@{baseline="$taskLinuxRoot/results/hrrn-native-stage4-20261026/bin/allocate-baseline.test";candidate="$taskLinuxOut/actions-allocate.test"}
 $taskRows=@()
 for ($taskRound=0;$taskRound -lt $PVCRounds;$taskRound++) {
  $taskOrder=if ($taskRound % 2 -eq 0) {@('baseline','candidate')} else {@('candidate','baseline')}
  foreach ($taskName in $taskOrder) {
   $taskLog=Join-Path $taskOut ("pvc-{0}-{1:d2}.jsonl" -f $taskName,$taskRound)
   & $taskGo tool test2json -t -p "pvc-$taskName" wsl.exe -d Codex-RF-Verify-20261019 --exec $taskBinaries[$taskName] '-test.v' '-test.count=1' '-test.run=^TestAllocateWithPVC$' '-test.timeout=60s' > $taskLog
   $taskRows+=@{round=$taskRound;branch=$taskName;exit_code=$LASTEXITCODE;log=[IO.Path]::GetFileName($taskLog)}
  }
  Write-Output "PVC paired round $($taskRound+1)/$PVCRounds recorded"
 }
 $taskRows | ConvertTo-Json | Set-Content (Join-Path $taskOut 'pvc-paired-results.json')
 Get-FileHash -Algorithm SHA256 (Join-Path $taskOld 'bin/allocate-baseline.test'),(Join-Path $taskOut 'actions-allocate.test'),(Join-Path $taskOld 'bin/vc-scheduler-hrrn') | ConvertTo-Json | Set-Content (Join-Path $taskOut 'binaries.json')
 & git diff --exit-code -- pkg/scheduler/actions/allocate/allocate_test.go > (Join-Path $taskOut 'pvc-test-unchanged.diff')
 if ($LASTEXITCODE -ne 0) {throw 'Original PVC assertion source changed'}
 @{hrrn_exit_code=$taskCoreExit;pvc_rounds_per_branch=$PVCRounds;pvc_failures=@($taskRows | Where-Object {$_.exit_code -ne 0}).Count;real_cluster_executed=$false} | ConvertTo-Json | Set-Content (Join-Path $taskOut 'summary.json')
 if ($taskCoreExit -ne 0) {throw 'HRRN native tests failed; see preserved evidence'}
 Write-Output 'HRRN passed; PVC paired results are reported separately without hiding failures'
} finally {
 Pop-Location
 if (-not $taskProcess.HasExited) {Stop-Process -Id $taskProcess.Id}
}
