# 部署到 47.114.33.192
# 用法：右键 → 使用 PowerShell 运行，或在终端执行 .\deploy\deploy.ps1

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$py = "C:\Users\Tuilge\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "找不到带 paramiko 的 python：$py" -ForegroundColor Red
    exit 1
}

# 有未提交的改动就提醒一下，但不拦
$dirty = (git -C $repo status --porcelain | Measure-Object -Line).Lines
if ($dirty -gt 0) {
    Write-Host "注意：还有 $dirty 个文件没提交" -ForegroundColor Yellow
}

Write-Host "开始部署（只传代码，数据库不动）..." -ForegroundColor Cyan
& $py (Join-Path $PSScriptRoot "deploy.py")
