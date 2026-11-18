param(
    [ValidateSet('Start', 'Status', 'Stop', 'Menu', 'Web')]
    [string]$Action = 'Start'
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

if ($Action -eq 'Menu') {
    while ($true) {
        Write-Host ''
        Write-Host '========= Weather99 模拟盘 ========='
        Write-Host '1. 启动模拟盘'
        Write-Host '2. 查看运行状态'
        Write-Host '3. 停止模拟盘'
        Write-Host '4. 打开网页监控（真实天气与盘口）'
        Write-Host '0. 退出菜单（后台继续运行）'
        $selection = Read-Host '请选择 [1/2/3/4/0]，输入后按回车'
        if ($null -eq $selection -or $selection -eq '0') { exit 0 }
        $selectedAction = switch ($selection.Trim()) {
            '1' { 'Start' }
            '2' { 'Status' }
            '3' { 'Stop' }
            '4' { 'Web' }
            default { $null }
        }
        if (-not $selectedAction) {
            Write-Host '请输入 1、2、3、4 或 0。' -ForegroundColor Yellow
            continue
        }
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath -Action $selectedAction
        if ($LASTEXITCODE -ne 0) {
            Write-Host "操作失败，退出码：$LASTEXITCODE" -ForegroundColor Red
        }
        Read-Host '按回车返回菜单' | Out-Null
    }
}

try {
    $pythonCandidates = @(
        (Join-Path $projectRoot '.venv\Scripts\python.exe'),
        'python',
        'py'
    )
    $pythonExe = $null
    foreach ($candidate in $pythonCandidates) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        $resolved = & $command.Source -c 'import sys; print(sys.executable); sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $resolved) {
            $pythonExe = ($resolved | Select-Object -Last 1).Trim()
            break
        }
    }
    if (-not $pythonExe) { throw 'Install Python 3.11 or newer, then run start.bat again.' }

    if ($Action -eq 'Stop') {
        $runDir = Join-Path $projectRoot 'runtime\paper'
        New-Item -ItemType Directory -Path $runDir -Force | Out-Null
        New-Item -ItemType File -Path (Join-Path $runDir 'stop.request') -Force | Out-Null
        Write-Host 'Stop requested. The runner exits after its current requests finish.'
        exit 0
    }

    $dependencyProbe = @'
import importlib.util, sys
sys.exit(0 if all(importlib.util.find_spec(name) for name in ('numpy', 'cryptography')) else 1)
'@
    & $pythonExe -c $dependencyProbe
    if ($LASTEXITCODE -ne 0) {
        if ($Action -eq 'Status') { throw 'Dependencies are missing. Run start.bat first.' }
        Write-Host 'Installing Weather99 dependencies...'
        & $pythonExe -m pip install -e '.[qweather-jwt]'
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check your Python/pip network settings.' }
    }

    if ($Action -eq 'Web') {
        $webUrl = 'http://127.0.0.1:8765'
        $webReady = $false
        try {
            $webData = Invoke-RestMethod -Uri "$webUrl/api/dashboard" -TimeoutSec 4
            $webReady = $webData.schema -eq 'weather99.dashboard.v1'
        } catch { }
        if (-not $webReady) {
            $webLogDir = Join-Path $projectRoot 'runtime\web'
            New-Item -ItemType Directory -Path $webLogDir -Force | Out-Null
            $webTag = [guid]::NewGuid().ToString('N').Substring(0, 8)
            $webErrorLog = Join-Path $webLogDir "web-$webTag.stderr.log"
            $webProcess = Start-Process -FilePath $pythonExe -ArgumentList '-u', '-m', 'weather99', 'web' -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $webLogDir "web-$webTag.stdout.log") -RedirectStandardError $webErrorLog -PassThru
            for ($webAttempt = 0; $webAttempt -lt 15; $webAttempt++) {
                Start-Sleep -Seconds 1
                $webProcess.Refresh()
                if ($webProcess.HasExited) { throw "网页服务未启动，请检查端口 8765 和日志 $webErrorLog" }
                try {
                    $webData = Invoke-RestMethod -Uri "$webUrl/api/dashboard" -TimeoutSec 4
                    if ($webData.schema -eq 'weather99.dashboard.v1') { $webReady = $true; break }
                } catch { }
            }
        }
        if (-not $webReady) { throw '网页服务暂未就绪，请稍后重试。' }
        Start-Process $webUrl
        Write-Host "网页监控已打开：$webUrl"
        Write-Host '真实天气与盘口来自当前采集记录；资金与成交来自本地模拟账本。'
        Write-Host '关闭网页不会停止采集。若数据停止更新，在菜单选择 1 启动采集。'
        exit 0
    }

    if ($Action -eq 'Status') {
        & $pythonExe -X utf8 -m weather99 paper status --human
        if ($LASTEXITCODE -ne 0) { throw 'Could not read paper runner status.' }
        exit 0
    }

    # Probe the account lock rather than trusting a possibly stale status file.
    $lockProbe = @'
import sys
from weather99.paper import SingleInstance, DEFAULT_RUN_DIR
try:
    with SingleInstance(DEFAULT_RUN_DIR / 'runner.lock'):
        pass
except ValueError:
    sys.exit(10)
'@
    & $pythonExe -c $lockProbe
    $probeResult = $LASTEXITCODE
    if ($probeResult -eq 10) {
        Write-Host '模拟盘已经在运行，无需重复启动。'
        & $pythonExe -X utf8 -m weather99 paper status --human
        exit $LASTEXITCODE
    }
    if ($probeResult -ne 0) { throw 'Could not check the account lock.' }

    $registry = Join-Path $projectRoot 'vendor\PolyWeather\src\data_collection\city_registry.py'
    if (-not (Test-Path -LiteralPath $registry)) {
        & git submodule update --init --recursive
        if ($LASTEXITCODE -ne 0) { throw 'Could not initialize the PolyWeather submodule.' }
    }
    & $pythonExe -c 'from weather99.qweather import QWeatherConfig; QWeatherConfig.from_env(required=True)'
    if ($LASTEXITCODE -ne 0) { throw 'Configure QWeather credentials before starting. See README.md.' }

    $runDir = Join-Path $projectRoot 'runtime\paper'
    New-Item -ItemType Directory -Path $runDir -Force | Out-Null
    $tag = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
    $stdout = Join-Path $runDir "runner-$tag.stdout.log"
    $stderr = Join-Path $runDir "runner-$tag.stderr.log"
    $runner = Start-Process -FilePath $pythonExe -ArgumentList '-u', '-m', 'weather99', 'paper', 'run' -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru

    # Wait for this process to write its own heartbeat; old status is not evidence.
    for ($attempt = 0; $attempt -lt 15; $attempt++) {
        Start-Sleep -Seconds 1
        $runner.Refresh()
        if ($runner.HasExited) {
            Get-Content -LiteralPath $stderr -Tail 20
            throw 'Runner exited during startup. See the log above.'
        }
        $statePath = Join-Path $runDir 'status.json'
        if (Test-Path -LiteralPath $statePath) {
            $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
            if ($state.pid -eq $runner.Id -and $state.phase -ne 'stopped') {
                Write-Host "Weather99 paper runner started. PID: $($runner.Id)"
                Write-Host 'You can close this window; the runner stays in the background.'
                Write-Host 'Open start.bat: choose 2 for status, or 3 for a graceful stop.'
                Write-Host "Output log: $stdout"
                Write-Host "Error log:  $stderr"
                exit 0
            }
        }
    }
    throw "Startup is not confirmed. Inspect $stderr and choose status in start.bat before retrying."
} catch {
    Write-Host "Weather99: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
