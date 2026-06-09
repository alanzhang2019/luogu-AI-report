# ============================================================================
# luogu-AI-report 本地构建 + 推送（Windows）
# 用法（在 PowerShell 里）：
#   .\build-and-ship.ps1                                      # 默认：build 当前分支 -> tar -> scp -> ssh 调 deploy-image.sh
#   .\build-and-ship.ps1 -Branch testing                     # 切到 testing 分支后 build
#   .\build-and-ship.ps1 -SkipBuild                          # 跳过 build，复用现成 tar
#   .\build-and-ship.ps1 -OnlyTar                            # 只 build + save tar，不上传
#   .\build-and-ship.ps1 -Server user@1.2.3.4                # 指定服务器
#   .\build-and-ship.ps1 -TagSuffix my-custom                # 给镜像 tag 加后缀（默认自动用分支+hash）
#
# 与 deploy.ps1（zip 模式）对比：
#   - deploy.ps1:  本地 zip -> scp -> 服务器 unzip -> 服务器 docker build (3-5min/次)
#   - build-and-ship.ps1: 本地 docker build (3-5min) -> save tar -> scp -> 服务器 docker load (5s) -> docker run
#     → 第二次部署只重 build 改动层，服务器端秒级
# ============================================================================

[CmdletBinding()]
param(
    [string]$Server = "ubuntu@43.163.26.115",
    [string]$RemoteDir = "/home/ubuntu/luogu-ai-report",
    [string]$Branch = "",            # 留空 = 当前分支
    [string]$TagSuffix = "",         # 留空 = 自动用 <branch>-<hash>
    [switch]$SkipBuild,
    [switch]$OnlyTar,
    [switch]$NoPush,                # build + tar + 留本地，不上传
    [switch]$DryRun                 # 干跑：打印所有会做的事，不真 build/save/scp
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot

# ---------- 颜色 ----------
function Write-Step($msg) { Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "[OK] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "[WARN] $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "[ERR] $msg" -ForegroundColor Red }

# ---------- 工具函数 ----------

function Test-Docker {
    Write-Step "检查 docker 可用性..."
    if ($DryRun) {
        Write-Warn "DryRun 模式：跳过 docker daemon 检查"
        return
    }
    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $docker) {
        Write-Err "找不到 docker 命令"
        Write-Warn "安装 Docker Desktop: https://www.docker.com/products/docker-desktop"
        exit 1
    }
    try {
        $info = docker info 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0) { throw "docker info 失败" }
    } catch {
        Write-Err "Docker daemon 不可用：$($_.Exception.Message)"
        Write-Warn "Windows 上需要在管理员 PowerShell 跑（Docker Desktop 需要 elevated shell）"
        Write-Warn "如需快速预览整个流程，先启 Docker Desktop 后再跑，或加 -DryRun 走 mock 路径"
        exit 1
    }
    Write-OK "docker OK"
}

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

function Get-BranchAndHash {
    if ($Branch) {
        Write-Step "切到分支: $Branch"
        git checkout $Branch
        if ($LASTEXITCODE -ne 0) { Write-Err "git checkout 失败"; exit 1 }
    }
    $currentBranch = (git rev-parse --abbrev-ref HEAD).Trim()
    $currentHash   = (git rev-parse --short HEAD).Trim()
    if ([string]::IsNullOrEmpty($currentBranch) -or [string]::IsNullOrEmpty($currentHash)) {
        Write-Err "无法获取 git branch/hash（当前不是 git 仓库？）"
        exit 1
    }
    return @($currentBranch, $currentHash)
}

function New-Image {
    param(
        [string]$ImageTag,
        [string]$TarPath
    )

    Write-Step "docker build -t $ImageTag ..."
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    docker build -t $ImageTag -f Dockerfile .
    if ($LASTEXITCODE -ne 0) { Write-Err "docker build 失败"; exit 1 }
    $sw.Stop()
    Write-OK "build 完成（$($sw.Elapsed.ToString('mm\:ss')))"

    Write-Step "打 latest 别名: $ImageTag -> luogu-ai-report/webapp:latest"
    $baseTag = $ImageTag.Split(':')[0]
    docker tag $ImageTag "${baseTag}:latest"

    Write-Step "导出镜像: docker save -> $TarPath"
    if (Test-Path $TarPath) { Remove-Item $TarPath -Force }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    docker save -o $TarPath $ImageTag
    if ($LASTEXITCODE -ne 0) { Write-Err "docker save 失败"; exit 1 }
    $sw.Stop()
    $size = [math]::Round((Get-Item $TarPath).Length / 1MB, 2)
    Write-OK "save 完成（$($sw.Elapsed.ToString('mm\:ss'))，$size MB）"
}

function Send-Tar {
    param([string]$TarPath)

    Write-Step "scp $TarPath → ${Server}:${RemoteDir}/"
    ssh $Server "mkdir -p $RemoteDir" | Out-Null
    scp $TarPath "${Server}:${RemoteDir}/webapp-image.tar"
    if ($LASTEXITCODE -ne 0) { Write-Err "scp 失败"; exit 1 }
    Write-OK "传输完成"
}

# ---------- 主流程 ----------

Test-Docker
Test-Ssh

# 1. 拿分支+hash
$bh = Get-BranchAndHash
$currentBranch = $bh[0]
$currentHash   = $bh[1]
Write-OK "分支=$currentBranch  hash=$currentHash"

# 2. 算 tag 和 tar 路径
$branchSafe = $currentBranch -replace '[^a-zA-Z0-9._-]', '_'
if ($TagSuffix) {
    $imageTag = "luogu-ai-report/webapp:${branchSafe}-${TagSuffix}"
} else {
    $imageTag = "luogu-ai-report/webapp:${branchSafe}-${currentHash}"
}
$tarName = "webapp-${branchSafe}-${currentHash}.tar"
$tarPath = Join-Path $env:USERPROFILE "Desktop\$tarName"

Write-Step "image tag: $imageTag"
Write-Step "tar 路径:  $tarPath"

# 3. build + save
if ($DryRun) {
    Write-Warn "DryRun：不真 build，只打印计划"
    Write-Host "  → docker build -t $imageTag -f Dockerfile ."
    Write-Host "  → docker tag  $imageTag luogu-ai-report/webapp:latest"
    Write-Host "  → docker save -o $tarPath $imageTag"
    Write-Host "  → scp $tarPath ${Server}:${RemoteDir}/webapp-image.tar"
    Write-Host "  → ssh $Server '$RemoteDir/deploy-image.sh up --tar webapp-image.tar --tag $imageTag'"
    Write-OK "DryRun 结束（未真执行任何 docker/scp/ssh 命令）"
    exit 0
}
if (-not $SkipBuild) {
    New-Image -ImageTag $imageTag -TarPath $tarPath
} else {
    if (-not (Test-Path $tarPath)) {
        Write-Err "SkipBuild 模式但找不到 $tarPath"
        exit 1
    }
    Write-OK "复用现有 tar: $tarPath ($([math]::Round((Get-Item $tarPath).Length / 1MB, 2)) MB)"
}

if ($OnlyTar -or $NoPush) {
    Write-OK "只 build + save，tar 留在: $tarPath"
    if ($OnlyTar) { exit 0 }
    if ($NoPush) { Write-OK "不推送，结束"; exit 0 }
}

# 4. 推送
Send-Tar -TarPath $tarPath

# 5. 服务器端部署
Write-Step "ssh 调远程 deploy-image.sh up --tar webapp-image.tar --tag $imageTag"
Invoke-Remote "cd $RemoteDir && chmod +x deploy-image.sh && ./deploy-image.sh up --tar webapp-image.tar --tag $imageTag"

Write-OK "部署完成 - $imageTag"
Write-Host ""
Write-Host "  服务器操作："
Write-Host "    查看状态: ssh $Server 'cd $RemoteDir && ./deploy-image.sh status'"
Write-Host "    跟踪日志: ssh $Server 'cd $RemoteDir && ./deploy-image.sh logs'"
Write-Host "    回滚:     ssh $Server 'cd $RemoteDir && ./deploy-image.sh rollback'"
