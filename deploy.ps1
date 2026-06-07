# ============================================================================
# luogu-AI-report 一键部署脚本（Windows 客户端）
# 用法（在 PowerShell 里）：
#   .\deploy.ps1                                 # 默认：打包+scp+调用服务器 deploy.sh
#   .\deploy.ps1 -SkipBuild                      # 跳过打包（用现有 deploy-pkg.zip）
#   .\deploy.ps1 -OnlyZip                        # 只打包，不传
#   .\deploy.ps1 -Server user@1.2.3.4            # 指定服务器
#   .\deploy.ps1 -Status                         # 查看服务器状态
#   .\deploy.ps1 -Logs                           # 跟踪服务器日志
#   .\deploy.ps1 -Restart                        # 改 .env 后重启
#   .\deploy.ps1 -ResetPassword                  # 重置 admin 密码
#   .\deploy.ps1 -Rollback                       # 回滚
# ============================================================================

[CmdletBinding()]
param(
    [string]$Server = "ubuntu@43.163.26.115",
    [string]$RemoteDir = "/home/ubuntu/luogu-ai-report",
    [switch]$SkipBuild,
    [switch]$OnlyZip,
    [switch]$Status,
    [switch]$Logs,
    [switch]$Restart,
    [switch]$ResetPassword,
    [switch]$Rollback
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$ZipPath = Join-Path $env:USERPROFILE "Desktop\luogu-ai-report-pkg.zip"

# ---------- 颜色 ----------
function Write-Step($msg) { Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "✓ $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "⚠ $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "✗ $msg" -ForegroundColor Red }

# ---------- 工具函数 ----------
function Test-Ssh {
    Write-Step "检查 ssh/scp 可用性..."
    $ssh = Get-Command ssh -ErrorAction SilentlyContinue
    $scp = Get-Command scp -ErrorAction SilentlyContinue
    if (-not $ssh -or -not $scp) {
        Write-Err "需要 ssh 和 scp（Windows 10 1809+ 自带）"
        Write-Warn "升级到 Windows 10 1809+ 或安装 OpenSSH：Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0"
        exit 1
    }
}

function Invoke-Remote {
    param([string]$Cmd)
    Write-Step "ssh $Server → $Cmd"
    ssh $Server $Cmd
    if ($LASTEXITCODE -ne 0) {
        Write-Err "远程命令失败（exit=$LASTEXITCODE）"
        exit 1
    }
}

function New-Package {
    if ($SkipBuild) {
        if (-not (Test-Path $ZipPath)) {
            Write-Err "SkipBuild 模式但找不到 $ZipPath"
            exit 1
        }
        Write-OK "复用现有 zip: $ZipPath"
        return
    }

    Write-Step "清理旧 zip..."
    Remove-Item $ZipPath -ErrorAction SilentlyContinue

    Write-Step "打包项目（自动排除 .git / .env / 缓存 / 报告 / pdf）..."
    # 用临时目录 + robocopy 排除（更可靠）
    $staging = Join-Path $env:TEMP "luogu-staging-$(Get-Random)"
    New-Item -ItemType Directory -Path $staging | Out-Null

    # robocopy 镜像，/XD 排除目录，/XF 排除文件
    $excludeDirs = @('.git', '.source_cache', 'reports', '__pycache__', '.dbg', 'node_modules', '.idea', '.vscode')
    $excludeFiles = @('.env', 'tasks.db', 'luogu-ai-report-pkg.zip', 'deploy-pkg.zip', 'cookies.json')

    $robocopyArgs = @(
        "`"$ProjectRoot`"",
        "`"$staging`"",
        "/MIR", "/NJH", "/NJS", "/NC", "/NDL", "/NFL", "/NP"
    )
    foreach ($d in $excludeDirs) { $robocopyArgs += "/XD"; $robocopyArgs += "`"$d`"" }
    foreach ($f in $excludeFiles) { $robocopyArgs += "/XF"; $robocopyArgs += "`"$f`"" }

    # robocopy 退出码 0-7 是成功
    & robocopy @robocopyArgs | Out-Null
    if ($LASTEXITCODE -ge 8) { Write-Err "robocopy 失败"; exit 1 }

    # 删大文件（PDF 等），仅当存在时
    Get-ChildItem -Path $staging -Recurse -Include *.pdf -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
    Get-ChildItem -Path $staging -Recurse -Include *.pyc -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue

    # 打包
    Compress-Archive -Path "$staging\*" -DestinationPath $ZipPath -CompressionLevel Optimal
    Remove-Item -Recurse -Force $staging

    $size = [math]::Round((Get-Item $ZipPath).Length / 1MB, 2)
    Write-OK "打包完成: $ZipPath ($size MB)"
}

function Send-Package {
    Write-Step "scp $ZipPath → ${Server}:${RemoteDir}/"
    ssh $Server "mkdir -p $RemoteDir" | Out-Null
    scp $ZipPath "${Server}:${RemoteDir}/deploy-pkg.zip"
    if ($LASTEXITCODE -ne 0) { Write-Err "scp 失败"; exit 1 }
    Write-OK "传输完成"
}

# ---------- 主流程 ----------

if ($Status) {
    Test-Ssh
    Invoke-Remote "cd $RemoteDir && chmod +x deploy.sh && ./deploy.sh --status"
    exit 0
}

if ($Logs) {
    Test-Ssh
    Invoke-Remote "cd $RemoteDir && ./deploy.sh --logs"
    exit 0
}

if ($Restart) {
    Test-Ssh
    Invoke-Remote "cd $RemoteDir && ./deploy.sh --restart"
    exit 0
}

if ($ResetPassword) {
    Test-Ssh
    Invoke-Remote "cd $RemoteDir && ./deploy.sh --reset-password"
    exit 0
}

if ($Rollback) {
    Test-Ssh
    Invoke-Remote "cd $RemoteDir && ./deploy.sh --rollback"
    exit 0
}

# 默认：完整部署
Test-Ssh
New-Package

if ($OnlyZip) {
    Write-OK "只打包，不上传：$ZipPath"
    exit 0
}

Send-Package
Invoke-Remote "cd $RemoteDir && chmod +x deploy.sh && ./deploy.sh --from-zip"
Write-OK "部署完成 ✓"
