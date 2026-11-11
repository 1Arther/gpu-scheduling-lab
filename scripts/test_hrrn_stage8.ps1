param(
  [Parameter(Mandatory=$true)][string]$Attempt,
  [Parameter(Mandatory=$true)][string]$RFEndpoint,
  [string]$Distro='Codex-RF-Verify-20261019'
)
$ErrorActionPreference='Stop'
if ($Attempt -notmatch '^[a-z0-9][a-z0-9-]{0,63}$') { throw 'Attempt must be a unique lowercase name.' }
if ($RFEndpoint -notmatch '^http://[0-9.]+:[0-9]+/v1/rank$') { throw 'Use the local frozen RF bridge endpoint.' }
$taskRoot=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$taskSource=Join-Path $taskRoot 'scheduler/volcano-rf'
$taskParent=Join-Path $taskRoot 'results/hrrn-k8s-stage8-20261027'
$taskOut=Join-Path $taskParent $Attempt
if (Test-Path -LiteralPath $taskOut) { throw 'Attempt already exists; preserve earlier evidence.' }
New-Item -ItemType Directory -Path $taskOut -ErrorAction Stop | Out-Null
$taskGo=Join-Path $taskRoot '.tools/go1.26.3/go/bin/go.exe'
$env:GOMODCACHE=Join-Path $taskRoot '.tools/gomodcache'
$env:GOCACHE=Join-Path $taskRoot '.tools/gocache'
$env:GOPATH=Join-Path $taskRoot '.tools/gopath'
$env:GOTOOLCHAIN='local'; $env:GOMAXPROCS='2'
$env:GOOS='linux'; $env:GOARCH='amd64'; $env:CGO_ENABLED='0'
$env:TEMP=Join-Path $taskRoot '.tmp'; $env:TMP=$env:TEMP
$taskLinuxRoot='/mnt/c'+$taskRoot.Substring(2).Replace('\','/')
$taskLinuxOut="$taskLinuxRoot/results/hrrn-k8s-stage8-20261027/$Attempt"
$packages=@('plugins/hrrn','api/devices/nvidia/vgpu','plugins/hrrn/coordination','plugins/hrrn/uidgate','framework','actions/allocate','actions/preempt','actions/reclaim')
$failed=@()
Push-Location $taskSource
try {
  foreach ($package in $packages) {
    $name=$package.Replace('/','-')
    $binary=Join-Path $taskOut "$name.test"
    # Windows PowerShell 5 may wrap native stderr as a nonterminating error;
    # klog normally writes to stderr, so judge the native exit code instead.
    $previousPreference=$ErrorActionPreference
    $ErrorActionPreference='Continue'
    & $taskGo test -p 2 -c -o $binary "./pkg/scheduler/$package" *> (Join-Path $taskOut "$name-build.log")
    $buildExit=$LASTEXITCODE
    $ErrorActionPreference=$previousPreference
    if ($buildExit -ne 0) { $failed+="$package (build)"; continue }
    $ErrorActionPreference='Continue'
    & wsl.exe -d $Distro --cd "$taskLinuxRoot/scheduler/volcano-rf/pkg/scheduler/$package" --exec env `
      "RF_LIVE_ENDPOINT=$RFEndpoint" 'RF_REQUIRE_LINUX=1' `
      "HRRN_KUBELET_CHECKPOINT_FIXTURE=$taskLinuxRoot/results/live-r4-partial/evidence/checkpoints/order-b-hrrn-s5-live01-20261027-order-b-long-0-raw-c7a3c5cbccec419bb19bcd283891460c.json" `
      "RF_WINDOW_CASES=$taskLinuxRoot/k8s/round7-logical-window/policy-cases.json" `
      "RF_ABLATION_PLAN=$taskLinuxRoot/results/rf-ablation-20261019/plan.json" `
      "RF_ABLATION_OUTPUT=$taskLinuxOut" "HRRN_MANIFESTS=$taskLinuxRoot/k8s/hrrn-stage4" `
      "HRRN_RESULTS=$taskLinuxOut" "RF_DEMO_OUTPUT=$taskLinuxOut" `
      "$taskLinuxOut/$name.test" '-test.v' '-test.count=1' '-test.timeout=8m' *> (Join-Path $taskOut "$name-tests.log")
    $testExit=$LASTEXITCODE
    $ErrorActionPreference=$previousPreference
    if ($testExit -ne 0) { $failed+="$package (tests)"; Write-Output "FAIL: $package" }
    else { Write-Output "PASS: $package" }
  }
} finally { Pop-Location }
if ($failed.Count) { throw "Stage8 tests failed: $($failed -join ', '); logs preserved in $taskOut" }
Write-Output "Local Linux Go test logs: $taskOut"
