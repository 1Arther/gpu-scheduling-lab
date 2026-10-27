param(
 [string[]]$Packages=@('plugins/hrrn','framework','plugins/priority','plugins/gang','plugins/drf','plugins/proportion','plugins/deviceshare','plugins/predicates','actions/allocate','actions/enqueue','actions/backfill','api/devices/nvidia/vgpu'),
 [string]$Run='.',
 [switch]$SkipBuild,
 [string]$RFEndpoint='',
 [string]$Distro='Codex-RF-Verify-20261019',
 [bool]$IsolateAllocate=$false
)
$ErrorActionPreference='Stop'
$taskRoot=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$taskSource=Join-Path $taskRoot 'scheduler/volcano-rf'
$taskOut=Join-Path $taskRoot 'results/hrrn-native-stage4-20261026'
$taskGo=Join-Path $taskRoot '.tools/go1.26.3/go/bin/go.exe'
$env:GOMODCACHE=Join-Path $taskRoot '.tools/gomodcache'
$env:GOCACHE=Join-Path $taskRoot '.tools/gocache'
$env:GOPATH=Join-Path $taskRoot '.tools/gopath'
$env:GOTOOLCHAIN='local'
$env:GOPROXY='https://proxy.golang.org|https://goproxy.cn|direct'
$env:GOMAXPROCS='2'
$env:GOOS='linux';$env:GOARCH='amd64';$env:CGO_ENABLED='0'
$env:TEMP=Join-Path $taskRoot '.tmp';$env:TMP=$env:TEMP
New-Item -ItemType Directory -Force (Join-Path $taskOut 'bin'),$env:TEMP | Out-Null
$taskLinuxRoot='/mnt/c'+$taskRoot.Substring(2).Replace('\','/')
$taskLinuxOut="$taskLinuxRoot/results/hrrn-native-stage4-20261026"
$taskFailures=@()
if ($RFEndpoint) {$taskEndpoint=$RFEndpoint} else {
 $taskGateway=((& wsl.exe -d $Distro -- ip -4 route show default) -split '\s+')[2]
 $taskEndpoint="http://${taskGateway}:18083/v1/rank"
}
Push-Location $taskSource
try {
 foreach ($taskPackage in $Packages) {
  $taskName=$taskPackage.Replace('/','-')
  $taskBinary=Join-Path $taskOut "bin/$taskName.test"
  if (-not $SkipBuild) {
   & $taskGo test -p 2 -c -o $taskBinary "./pkg/scheduler/$taskPackage" > (Join-Path $taskOut "$taskName-build.log") 2>&1
   if ($LASTEXITCODE -ne 0) {throw "Build failed: $taskPackage"}
   if ((Get-Content (Join-Path $taskOut "$taskName-build.log") -Raw) -match '\[no test files\]') {
    @{Package=$taskPackage;Action='no_test_files'} | ConvertTo-Json -Compress | Set-Content (Join-Path $taskOut "$taskName-tests.jsonl")
    Write-Output "NO TEST FILES: $taskPackage (covered by integration where applicable)"
    continue
   }
  }
  # These Linux binaries are actually executed by the existing local WSL distro.
  # Compilation alone never counts as a passing test.
  if ($taskPackage -eq 'actions/allocate' -and $IsolateAllocate) {
   # An intermittent PVC/informer test failed in the broad suite. Preserve that
   # failure evidence; independent processes reduce unrelated fixture effects.
   # This is an isolation diagnostic, not proof of the failure's root cause.
   $taskTests=@(& wsl.exe -d $Distro -- "$taskLinuxOut/bin/$taskName.test" '-test.list=.' | Where-Object {$_ -match '^Test'})
   if (-not $taskTests.Count) {throw 'No allocate tests discovered'}
   $taskLog=Join-Path $taskOut "$taskName-isolated-tests.jsonl"
   Set-Content -LiteralPath $taskLog -Value ''
   foreach ($taskTest in $taskTests) {
    & $taskGo tool test2json -t -p "volcano.sh/volcano/pkg/scheduler/$taskPackage" wsl.exe -d $Distro --cd "$taskLinuxRoot/scheduler/volcano-rf/pkg/scheduler/$taskPackage" -- env "RF_LIVE_ENDPOINT=$taskEndpoint" "$taskLinuxOut/bin/$taskName.test" '-test.v' '-test.count=1' "-test.run=^${taskTest}$" '-test.timeout=8m' | Out-File -LiteralPath $taskLog -Append -Encoding utf8
    if ($LASTEXITCODE -ne 0) {throw "Isolated native test failed: $taskTest"}
   }
   Write-Output "PASS (separate process per top-level test): $taskPackage"
   continue
  }
  & $taskGo tool test2json -t -p "volcano.sh/volcano/pkg/scheduler/$taskPackage" wsl.exe -d $Distro --cd "$taskLinuxRoot/scheduler/volcano-rf/pkg/scheduler/$taskPackage" -- env "RF_LIVE_ENDPOINT=$taskEndpoint" 'RF_REQUIRE_LINUX=1' "RF_WINDOW_CASES=$taskLinuxRoot/k8s/round7-logical-window/policy-cases.json" "RF_ABLATION_PLAN=$taskLinuxRoot/results/rf-ablation-20261019/plan.json" "RF_ABLATION_OUTPUT=$taskLinuxOut" "HRRN_MANIFESTS=$taskLinuxRoot/k8s/hrrn-stage4" "HRRN_RESULTS=$taskLinuxOut" "RF_DEMO_OUTPUT=$taskLinuxOut" "$taskLinuxOut/bin/$taskName.test" '-test.v' '-test.count=1' "-test.run=$Run" '-test.timeout=8m' > (Join-Path $taskOut "$taskName-tests.jsonl")
  if ($LASTEXITCODE -ne 0) {$taskFailures+=$taskPackage; Write-Output "FAIL: $taskPackage (remaining packages will still run)";continue}
  Write-Output "PASS: $taskPackage"
 }
 if ($taskFailures.Count) {throw "Native tests failed: $($taskFailures -join ', '); see preserved JSONL evidence"}
} finally {Pop-Location}
