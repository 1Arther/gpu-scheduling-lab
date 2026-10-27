param([switch]$SkipBuild, [switch]$SkipPython)
$ErrorActionPreference = 'Stop'
$taskProject = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$taskSource = Join-Path $taskProject 'scheduler/volcano-rf'
$taskGo = Join-Path $taskProject '.tools/go1.26.3/go/bin/go.exe'
if (-not (Test-Path -LiteralPath $taskGo)) { $taskGo = (Get-Command go -ErrorAction Stop).Source }
$env:GOMODCACHE = Join-Path $taskProject '.tools/gomodcache'
$env:GOCACHE = Join-Path $taskProject '.tools/gocache'
$env:GOPATH = Join-Path $taskProject '.tools/gopath'
$env:GOPROXY = 'https://goproxy.cn|https://proxy.golang.org|direct'
$env:GOTOOLCHAIN = 'local'
$env:GOMAXPROCS = '2'
$env:CGO_ENABLED = '0'
$env:GIT_CONFIG_COUNT = '1'
$env:GIT_CONFIG_KEY_0 = 'safe.directory'
$env:GIT_CONFIG_VALUE_0 = $taskSource
$env:TEMP = Join-Path $taskProject '.tmp'
$env:TMP = $env:TEMP
$taskResults = Join-Path $taskProject 'results/integration-v6'
New-Item -ItemType Directory -Force $taskResults, $env:TEMP | Out-Null
$taskHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:18081/health' -TimeoutSec 5
if (-not $taskHealth.model_ready -or $taskHealth.model_version -ne 'rf-v4-known-states-ee35cfabe30f') {
    throw 'Start the pinned Python RF HTTP service before this script.'
}
$env:RF_LIVE_ENDPOINT = 'http://127.0.0.1:18081/v1/rank'
$env:RF_DEMO_OUTPUT = $taskResults
Push-Location $taskSource
try {
    $taskPackages = @('./pkg/scheduler/api/devices/nvidia/vgpu')
    if (-not $IsWindows) { $taskPackages += './pkg/scheduler/plugins/deviceshare', './pkg/scheduler/plugins/predicates' }
    & $taskGo test -p 2 -count=1 -json @taskPackages > (Join-Path $taskResults 'go-tests.jsonl')
    if ($LASTEXITCODE -ne 0) { throw 'Go tests failed; inspect go-tests.jsonl.' }
    if (-not $SkipBuild) {
        $env:GOOS = 'linux'; $env:GOARCH = 'amd64'
        New-Item -ItemType Directory -Force (Join-Path $taskResults 'bin') | Out-Null
        $taskHead = git rev-parse HEAD
        if ($taskHead -notmatch '^[a-f0-9]{40}$') { throw 'Cannot identify local source commit.' }
        & $taskGo build -p 2 -ldflags "-X volcano.sh/volcano/pkg/version.Version=v1.13.1-rf-job-offline -X volcano.sh/volcano/pkg/version.GitSHA=$taskHead" -o (Join-Path $taskResults 'bin/vc-scheduler-rf-linux-amd64') ./cmd/scheduler
        if ($LASTEXITCODE -ne 0) { throw 'Linux scheduler build failed.' }
        & $taskGo version -m (Join-Path $taskResults 'bin/vc-scheduler-rf-linux-amd64') > (Join-Path $taskResults 'binary-build-info.txt')
        if ($IsWindows) {
            Write-Warning 'Upstream unix.Umask prevents native Windows plugin tests; compile Linux test executables, do not claim they ran.'
            foreach ($taskPlugin in @('deviceshare', 'predicates')) {
                & $taskGo test -p 2 -c -o (Join-Path $taskResults "bin/$taskPlugin-linux.test") "./pkg/scheduler/plugins/$taskPlugin"
                if ($LASTEXITCODE -ne 0) { throw "Linux test compilation failed: $taskPlugin" }
            }
        }
    }
} finally {
    Remove-Item Env:GOOS -ErrorAction SilentlyContinue
    Remove-Item Env:GOARCH -ErrorAction SilentlyContinue
    Pop-Location
}
if (-not $SkipPython) {
    Push-Location $taskProject
    try {
        & (Join-Path $taskProject '.venv/Scripts/python.exe') -m pytest -q --junitxml=results/integration-v6/pytest-all.xml
        if ($LASTEXITCODE -ne 0) { throw 'Python tests failed.' }
    } finally { Pop-Location }
}
Write-Output "Offline tests complete: $taskResults (no cluster deployment)."
