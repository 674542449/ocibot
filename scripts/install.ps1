# OCIBot one-click install / update for Windows (Docker Desktop + PowerShell)
#
#   irm https://raw.githubusercontent.com/<OWNER>/<REPO>/master/scripts/install.ps1 | iex
#   .\scripts\install.ps1 install
#   .\scripts\install.ps1 update
#
param(
  [ValidateSet("install", "update", "status", "uninstall")]
  [string]$Command = "install",
  [string]$RepoUrl = $env:OCIBOT_REPO_URL,
  [string]$RepoDir = $(if ($env:OCIBOT_DIR) { $env:OCIBOT_DIR } else { Join-Path $HOME "ocibot" }),
  [string]$Branch = $(if ($env:OCIBOT_BRANCH) { $env:OCIBOT_BRANCH } else { "main" }),
  [int]$Port = $(if ($env:OCIBOT_PORT) { [int]$env:OCIBOT_PORT } else { 8000 })
)

$ErrorActionPreference = "Stop"

function Write-Info($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Warn($msg) { Write-Host "!!  $msg" -ForegroundColor Yellow }

function Test-Docker {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "未找到 Docker。请安装 Docker Desktop 后重试：https://www.docker.com/products/docker-desktop/"
  }
  docker compose version | Out-Null
}

function New-RandomHex([int]$Bytes = 48) {
  $buf = New-Object byte[] $Bytes
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buf)
  -join ($buf | ForEach-Object { $_.ToString("x2") })
}

function Ensure-Repo {
  if (Test-Path (Join-Path $RepoDir ".git")) {
    Write-Info "已有仓库: $RepoDir"
    return
  }
  $here = Split-Path -Parent $PSScriptRoot
  if ((Test-Path (Join-Path $here "docker-compose.yml")) -and (Test-Path (Join-Path $here "web"))) {
    $script:RepoDir = $here
    Write-Info "使用当前目录: $RepoDir"
    return
  }
  if (-not $RepoUrl) {
    throw "请设置 -RepoUrl 或环境变量 OCIBOT_REPO_URL，或在仓库目录内运行"
  }
  if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "需要 git"
  }
  Write-Info "克隆 $RepoUrl → $RepoDir"
  git clone --branch $Branch --depth 1 $RepoUrl $RepoDir
}

function Ensure-Env {
  $envf = Join-Path $RepoDir "web\.env"
  if (Test-Path $envf) {
    Write-Info "保留已有 web\.env"
    return
  }
  Write-Info "生成 web\.env（随机密钥）"
  New-Item -ItemType Directory -Force -Path (Split-Path $envf) | Out-Null
  $pg = (New-RandomHex 16)
  $master = (New-RandomHex 48)
  $jwt = (New-RandomHex 48)
  @"
POSTGRES_PASSWORD=$pg
OCIBOT_MASTER_KEY=$master
OCIBOT_JWT_SECRET=$jwt
OCIBOT_REQUIRE_SECURE_SECRETS=1
OCIBOT_CORS_ORIGINS=http://127.0.0.1:$Port,http://localhost:$Port
OCIBOT_COOKIE_SECURE=0
OCIBOT_COOKIE_SAMESITE=lax
OCIBOT_ALLOW_OPEN_REGISTRATION=0
OCIBOT_JWT_EXPIRE_MINUTES=720
OCIBOT_API_WORKERS=2
OCIBOT_DB_POOL_SIZE=10
OCIBOT_DB_MAX_OVERFLOW=20
OCIBOT_PORT=$Port
"@ | Set-Content -Path $envf -Encoding utf8
  Write-Warn "已写入随机密钥到 web\.env — 请备份；丢失 OCIBOT_MASTER_KEY 将无法解密租户私钥"
}

function Invoke-Compose([string[]]$Args) {
  Push-Location $RepoDir
  try {
    & docker compose @Args
    if ($LASTEXITCODE -ne 0) { throw "docker compose failed: $Args" }
  } finally {
    Pop-Location
  }
}

switch ($Command) {
  "install" {
    Test-Docker
    Ensure-Repo
    Ensure-Env
    Write-Info "构建并启动（PostgreSQL + API + Worker）…"
    Invoke-Compose @("up", "-d", "--build")
    Write-Info "就绪后打开 http://127.0.0.1:$Port 注册管理员"
  }
  "update" {
    Test-Docker
    Ensure-Repo
    if (Test-Path (Join-Path $RepoDir ".git")) {
      Write-Info "拉取最新代码…"
      git -C $RepoDir fetch --depth 1 origin $Branch
      git -C $RepoDir checkout $Branch
      try { git -C $RepoDir pull --ff-only origin $Branch } catch { Write-Warn "git pull 跳过" }
    }
    Ensure-Env
    Write-Info "重新构建并更新…"
    Invoke-Compose @("up", "-d", "--build")
  }
  "status" {
    Test-Docker
    Ensure-Repo
    Invoke-Compose @("ps")
    try {
      (Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/health" -UseBasicParsing).Content
    } catch {
      Write-Warn "API 未响应"
    }
  }
  "uninstall" {
    Test-Docker
    Ensure-Repo
    Write-Warn "停止容器（默认保留数据卷）"
    if ($env:OCIBOT_PURGE_DATA -eq "1") {
      Invoke-Compose @("down", "-v")
    } else {
      Invoke-Compose @("down")
    }
  }
}
