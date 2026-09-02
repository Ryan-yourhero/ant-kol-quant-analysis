# 蚂蚁财富大V操作采集 - 启动脚本
# 自动检测 MySQL、启动服务、执行爬虫

$ErrorActionPreference = "Stop"
$PROJECT_ROOT = Split-Path -Parent (Split-Path -Parent $PSCommandPath)

Set-Location $PROJECT_ROOT

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  蚂蚁财富大V操作采集" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

# ---- 1. 检查 MySQL80 服务 ----
$MYSQL_SERVICE = "MySQL80"
$service = Get-Service -Name $MYSQL_SERVICE -ErrorAction SilentlyContinue

if (-not $service) {
    Write-Host "[WARN] 未找到服务 $MYSQL_SERVICE，跳过 MySQL 检测" -ForegroundColor Yellow
} elseif ($service.Status -ne "Running") {
    Write-Host "[INFO] $MYSQL_SERVICE 未启动，正在启动..." -ForegroundColor Yellow
    Start-Service -Name $MYSQL_SERVICE
    Write-Host "[OK] $MYSQL_SERVICE 已启动" -ForegroundColor Green
} else {
    Write-Host "[OK] $MYSQL_SERVICE 已在运行" -ForegroundColor Green
}

# ---- 2. 等待 MySQL 可连接 ----
Write-Host "[INFO] 等待 MySQL 就绪..." -ForegroundColor Cyan

$maxRetries = 30
$retry = 0
while ($retry -lt $maxRetries) {
    try {
        $conn = New-Object System.Net.Sockets.TcpClient
        $conn.Connect("localhost", 3306)
        if ($conn.Connected) {
            $conn.Close()
            Write-Host "[OK] MySQL 已就绪" -ForegroundColor Green
            break
        }
    } catch {
        # not ready yet
    }
    $retry++
    Start-Sleep -Seconds 1
}

if ($retry -ge $maxRetries) {
    Write-Host "[WARN] MySQL 连接超时，继续执行（可能影响数据库写入）" -ForegroundColor Yellow
}

# ---- 3. 执行主程序 ----
Write-Host "[INFO] 启动爬虫..." -ForegroundColor Cyan
Write-Host ""

python main.py

Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  完成" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
