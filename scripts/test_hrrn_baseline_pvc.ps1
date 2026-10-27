$ErrorActionPreference='Stop'
$taskRoot=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$taskOut=Join-Path $taskRoot 'results/hrrn-native-stage4-20261026'
$taskGo=Join-Path $taskRoot '.tools/go1.26.3/go/bin/go.exe'
$env:GOMODCACHE=Join-Path $taskRoot '.tools/gomodcache';$env:GOCACHE=Join-Path $taskRoot '.tools/gocache'
$env:GOPATH=Join-Path $taskRoot '.tools/gopath';$env:GOTOOLCHAIN='local';$env:GOMAXPROCS='2'
$env:GOOS='linux';$env:GOARCH='amd64';$env:CGO_ENABLED='0'
& (Join-Path $taskRoot '.venv/Scripts/python.exe') (Join-Path $PSScriptRoot 'hrrn_baseline_overlay.py')
Push-Location (Join-Path $taskRoot 'scheduler/volcano-rf')
try {
 & $taskGo test -p 2 -overlay (Join-Path $taskOut 'baseline-overlay/overlay.json') -c -o (Join-Path $taskOut 'bin/allocate-baseline.test') ./pkg/scheduler/actions/allocate > (Join-Path $taskOut 'allocate-baseline-build.log') 2>&1
 if ($LASTEXITCODE -ne 0) {throw 'Baseline overlay build failed'}
 $taskLinuxOut='/mnt/c'+$taskOut.Substring(2).Replace('\','/')
 & $taskGo tool test2json -t -p allocate-baseline wsl.exe -d Codex-RF-Verify-20261019 -- "$taskLinuxOut/bin/allocate-baseline.test" '-test.v' '-test.count=1' '-test.timeout=8m' > (Join-Path $taskOut 'allocate-baseline-full.jsonl')
 $taskFullExit=$LASTEXITCODE
 & $taskGo tool test2json -t -p allocate-baseline-pvc-isolated wsl.exe -d Codex-RF-Verify-20261019 -- "$taskLinuxOut/bin/allocate-baseline.test" '-test.v' '-test.count=1' '-test.run=^TestAllocateWithPVC$' > (Join-Path $taskOut 'allocate-baseline-pvc-isolated.jsonl')
 @{baselineFullExit=$taskFullExit;baselinePVCIsolatedExit=$LASTEXITCODE} | ConvertTo-Json | Set-Content (Join-Path $taskOut 'baseline-pvc-status.json')
} finally {Pop-Location}
