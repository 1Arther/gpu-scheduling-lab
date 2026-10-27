param([string]$OutputName='vc-scheduler-hrrn')
$ErrorActionPreference='Stop'
$taskRoot=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$env:GOMODCACHE=Join-Path $taskRoot '.tools/gomodcache'
$env:GOCACHE=Join-Path $taskRoot '.tools/gocache'
$env:GOPATH=Join-Path $taskRoot '.tools/gopath'
$env:GOTOOLCHAIN='local';$env:GOMAXPROCS='2'
$env:GOOS='linux';$env:GOARCH='amd64';$env:CGO_ENABLED='0'
$taskOut=Join-Path $taskRoot 'results/hrrn-native-stage4-20261026'
Push-Location (Join-Path $taskRoot 'scheduler/volcano-rf')
try {
 & (Join-Path $taskRoot '.tools/go1.26.3/go/bin/go.exe') build -p 2 -trimpath -o (Join-Path $taskOut "bin/$OutputName") ./cmd/scheduler > (Join-Path $taskOut 'scheduler-build.log') 2>&1
 if ($LASTEXITCODE -ne 0) {throw 'Native scheduler build failed'}
 Get-FileHash (Join-Path $taskOut "bin/$OutputName") -Algorithm SHA256 | ConvertTo-Json | Set-Content (Join-Path $taskOut 'scheduler-binary-sha.json')
 & (Join-Path $taskRoot '.tools/go1.26.3/go/bin/go.exe') version -m (Join-Path $taskOut "bin/$OutputName") > (Join-Path $taskOut 'binary-build-info.txt')
 $taskInputs=@('pkg/scheduler/plugins/hrrn/hrrn.go','pkg/scheduler/plugins/hrrn/rollback.go','pkg/scheduler/plugins/factory.go','pkg/scheduler/framework/job_plan_guard.go','pkg/scheduler/framework/session.go','pkg/scheduler/framework/session_plugins.go')
 $taskInputs | ForEach-Object {Get-FileHash -LiteralPath $_ -Algorithm SHA256} | ConvertTo-Json | Set-Content (Join-Path $taskOut 'binary-source-inputs.json')
} finally {Pop-Location}
