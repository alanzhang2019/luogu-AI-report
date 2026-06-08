#!/usr/bin/env bash
# ============================================================================
# luogu-AI-report 镜像化部署脚本（服务器端，配套 build-and-ship.ps1）
# 用法：
#   ./deploy-image.sh up --tar <path> --tag <image:tag>     # load + run
#   ./deploy-image.sh up --tag <image:tag>                 # 已有镜像，直接起
#   ./deploy-image.sh status                               # 容器/镜像/卷/健康
#   ./deploy-image.sh logs                                 # 跟踪日志
#   ./deploy-image.sh restart                              # 重启容器
#   ./deploy-image.sh stop                                 # 停容器（保留镜像）
#   ./deploy-image.sh rollback                             # 回滚到上一个镜像
#   ./deploy-image.sh images                               # 列出已 load 的镜像
#   ./deploy-image.sh prune [keep]                         # 清理旧镜像（默认保留最近 3 个）
#   ./deploy-image.sh --help                               # 帮助
#
# 与 docker-compose.yml 关系：
#   - 不走 compose（compose.yml 里有 build 块，up 会重 build）
#   - 用 docker run 直接起，env 从 .env 注入，卷与原 compose 一致
# ============================================================================

set -euo pipefail

# ---------- 颜色 ----------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

# ---------- 默认值 ----------
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="luogu-ai-report-luogu-coach"   # 与原 compose 服务同名
TAR_PATH=""
IMAGE_TAG=""
MODE=""
KEEP_N=3

# ---------- 帮助 ----------
usage() {
  sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

# ---------- 参数解析 ----------
SUBCMD="${1:-}"
shift || true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tar)   TAR_PATH="$2"; shift 2 ;;
    --tag)   IMAGE_TAG="$2"; shift 2 ;;
    --keep)  KEEP_N="$2"; shift 2 ;;
    --help|-h) usage ;;
    *)       echo -e "${RED}未知参数: $1${NC}"; usage ;;
  esac
done

# ---------- 工具函数 ----------
log()  { echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} $*"; }
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
err()  { echo -e "${RED}✗${NC} $*" >&2; }

need_root() {
  if [[ $EUID -ne 0 ]]; then
    err "请用 root 或 sudo 运行（首次需要写 /var/lib/docker）"
    exit 1
  fi
}

# 把 .env 转成 --env-file 兼容形式（k=v 行），作为 docker run --env 的入参
env_to_args() {
  [[ -f .env ]] || { err ".env 不存在"; exit 1; }
  # 跳过注释/空行，保留 k=v
  while IFS= read -r line; do
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    # 跳过 export 前缀
    line="${line#export }"
    if [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
      printf -- "-e %q " "$line"
    fi
  done < .env
}

# 拿 .env 里的端口（默认 5000）
env_port() {
  if [[ -f .env ]] && grep -qE '^[[:space:]]*PORT[[:space:]]*=' .env; then
    grep -E '^[[:space:]]*PORT[[:space:]]*=' .env | sed -E 's/.*=[[:space:]]*([0-9]+).*/\1/' | head -1
  else
    echo "5000"
  fi
}

# ---------- 子命令 ----------

cmd_status() {
  cd "$PROJECT_DIR"
  echo
  echo "=== 容器状态 ==="
  if docker ps -a --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep -E "NAMES|$APP_NAME" >/dev/null 2>&1; then
    docker ps -a --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep -E "NAMES|$APP_NAME"
  else
    echo "  (无)"
  fi
  echo
  echo "=== 已加载的 luogu-ai-report/webapp 镜像 ==="
  docker images luogu-ai-report/webapp --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedAt}}" 2>/dev/null | head -20
  echo
  echo "=== 卷 ==="
  docker volume ls | grep luogu-ai-report || echo "  (无)"
  echo
  echo "=== .env 状态 ==="
  if [[ -f "$PROJECT_DIR/.env" ]]; then
    echo "  存在，行数：$(wc -l < "$PROJECT_DIR/.env")"
    grep -E "ADMIN_PASSWORD|OPENAI_API_KEY" "$PROJECT_DIR/.env" | sed 's/=.*/=***已设置***/' | sed 's/^/    /'
  else
    warn "  .env 不存在！需要创建：cp .env.example .env"
  fi
  echo
  echo "=== 健康检查 ==="
  local port
  port=$(env_port)
  if curl -fsS --max-time 5 "http://127.0.0.1:${port}/" >/dev/null 2>&1; then
    ok "  http://127.0.0.1:${port}/ 可访问"
  else
    warn "  http://127.0.0.1:${port}/ 不可访问"
  fi
}

cmd_logs() {
  cd "$PROJECT_DIR"
  if docker ps -a --format '{{.Names}}' | grep -qx "$APP_NAME"; then
    docker logs -f --tail=200 "$APP_NAME"
  else
    err "容器 $APP_NAME 不存在"
    exit 1
  fi
}

cmd_restart() {
  cd "$PROJECT_DIR"
  if ! docker ps -a --format '{{.Names}}' | grep -qx "$APP_NAME"; then
    err "容器 $APP_NAME 不存在，请用 up 起"
    exit 1
  fi
  log "重启容器 $APP_NAME"
  docker restart "$APP_NAME"
  ok "已重启"
  sleep 5
  cmd_status
}

cmd_stop() {
  cd "$PROJECT_DIR"
  if ! docker ps -a --format '{{.Names}}' | grep -qx "$APP_NAME"; then
    ok "容器 $APP_NAME 已不存在"
    return
  fi
  log "停止容器 $APP_NAME"
  docker stop "$APP_NAME"
  ok "已停止"
}

cmd_images() {
  docker images luogu-ai-report/webapp --format "table {{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.Size}}\t{{.CreatedAt}}"
}

cmd_prune() {
  log "清理 luogu-ai-report/webapp 旧镜像，保留最近 $KEEP_N 个"
  # 列出除 latest 和 none 外的镜像，按创建时间排序，删旧的
  # 用 || true 兜底 pipefail（空 grep 会返回 1）
  local to_delete
  to_delete=$(docker images luogu-ai-report/webapp --format "{{.ID}} {{.CreatedAt}}" 2>/dev/null \
    | { grep -v "0001-01-01" || true; } \
    | sort -rk2 \
    | awk 'NR>'"$KEEP_N"' {print $1}' \
    | { grep -v '^$' || true; })
  if [[ -z "$to_delete" ]]; then
    ok "没有可清理的"
    return
  fi
  echo "$to_delete" | xargs -r docker rmi -f
  ok "清理完成"
  cmd_images
}

cmd_rollback() {
  cd "$PROJECT_DIR"
  warn "将回滚到上一个镜像版本"
  # 当前正在跑的镜像
  local current_image
  current_image=$(docker inspect --format='{{.Config.Image}}' "$APP_NAME" 2>/dev/null || echo "")
  if [[ -z "$current_image" ]]; then
    err "容器 $APP_NAME 未运行，无法定位当前镜像"
    exit 1
  fi
  log "当前镜像: $current_image"

  # 找其他可用的 luogu-ai-report/webapp:* 镜像（除当前）
  local candidates
  candidates=$(docker images luogu-ai-report/webapp --format "{{.Repository}}:{{.Tag}} {{.CreatedAt}}" 2>/dev/null \
    | { awk -v cur="$current_image" '$1 != cur && $1 != "luogu-ai-report/webapp:latest" && $1 != "luogu-ai-report/webapp:<none>:<none>"' || true; } \
    | sort -rk2)
  if [[ -z "$candidates" ]]; then
    err "找不到其他可用镜像"
    exit 1
  fi
  echo
  echo "可回滚的版本（按时间从新到旧）："
  echo "$candidates" | awk '{printf "  - %s  (%s)\n", $1, $2}'
  echo
  local target
  target=$(echo "$candidates" | head -1 | awk '{print $1}')
  warn "将回滚到: $target"
  read -rp "确认？[y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || { echo "已取消"; exit 0; }

  log "停容器 + 用新 tag 启动"
  docker stop "$APP_NAME" 2>/dev/null || true
  IMAGE_TAG="$target" cmd_up_internal
}

cmd_up() {
  cd "$PROJECT_DIR"
  [[ -z "$IMAGE_TAG" ]] && { err "up 必须指定 --tag"; exit 1; }

  if [[ -n "$TAR_PATH" ]]; then
    if [[ ! -f "$TAR_PATH" ]]; then
      err "找不到 tar 文件: $TAR_PATH"
      exit 1
    fi
    log "load 镜像: docker load -i $TAR_PATH"
    docker load -i "$TAR_PATH"
    # load 完用 image ID 查 repo:tag（tar 里可能有多个 tag）
    # 但通常我们用 --tag 显式指定的（load 后该 tag 已在）
    # 这里做一个兜底：如果指定 tag 不在本地，就用 latest
    if ! docker image inspect "$IMAGE_TAG" >/dev/null 2>&1; then
      warn "tar 里没有 tag=$IMAGE_TAG，尝试用 luogu-ai-report/webapp:latest"
      IMAGE_TAG="luogu-ai-report/webapp:latest"
    fi
  fi

  # 镜像必须本地存在
  if ! docker image inspect "$IMAGE_TAG" >/dev/null 2>&1; then
    err "本地没有镜像: $IMAGE_TAG"
    err "先用 --tar 加载，或确认 tag 拼写正确"
    exit 1
  fi

  cmd_up_internal
}

cmd_up_internal() {
  cd "$PROJECT_DIR"
  # .env 必填
  if [[ ! -f .env ]]; then
    err ".env 不存在！先 cp .env.example .env 并配置 OPENAI_API_KEY / ADMIN_PASSWORD / ADMIN_SESSION_SECRET"
    exit 1
  fi

  local port
  port=$(env_port)

  # 停掉旧容器（保留卷）
  if docker ps -a --format '{{.Names}}' | grep -qx "$APP_NAME"; then
    log "停掉旧容器 $APP_NAME"
    docker stop "$APP_NAME" 2>/dev/null || true
    docker rm -f "$APP_NAME" 2>/dev/null || true
  fi

  # 创建持久卷（如不存在）
  for vol in luogu-ai-report_tasks-data luogu-ai-report_source-cache; do
    docker volume create "$vol" >/dev/null 2>&1 || true
  done

  # 把 .env 注入为环境变量
  local env_args
  env_args=$(env_to_args)

  # 挂载 reports 目录（如存在），命名卷用 :rw
  local vol_args="-v luogu-ai-report_tasks-data:/app/data -v luogu-ai-report_source-cache:/app/.source_cache"
  if [[ -d "$PROJECT_DIR/reports" ]]; then
    vol_args="$vol_args -v $PROJECT_DIR/reports:/app/reports"
  fi

  log "启动: $IMAGE_TAG  端口 ${port}:${port}"
  # shellcheck disable=SC2086
  eval docker run -d \
    --name "$APP_NAME" \
    --restart unless-stopped \
    -p "${port}:${port}" \
    $env_args \
    $vol_args \
    "$IMAGE_TAG"

  ok "已启动"
  sleep 5
  cmd_status
}

# ---------- 主流程 ----------

case "$SUBCMD" in
  up)        need_root; cmd_up ;;
  status)    cmd_status ;;
  logs)      cmd_logs ;;
  restart)   need_root; cmd_restart ;;
  stop)      need_root; cmd_stop ;;
  rollback)  need_root; cmd_rollback ;;
  images)    cmd_images ;;
  prune)     need_root; cmd_prune ;;
  --help|-h|"") usage ;;
  *)         err "未知子命令: $SUBCMD"; usage ;;
esac
