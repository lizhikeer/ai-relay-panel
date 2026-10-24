#!/usr/bin/env bash
# =============================================================================
# ai-relay-panel 一键部署脚本（在 NAS 上运行）
#
#   ./deploy.sh deploy     构建镜像 → 起容器 → 等待健康 → 校验/备份/热加载 Caddy → 端到端校验
#   ./deploy.sh rollback   把 Caddy 恢复为上一份备份（默认最新备份），并验证原生面板可用
#   ./deploy.sh status     打印容器状态与各入口健康
#
# 设计要点：
#   - 容器用 network_mode: host，BFF 监听 127.0.0.1:18080
#   - Caddy 改动严格遵循：validate → 备份 → 原地写入 → reload --force（绝不 restart）
#   - Caddyfile 必须「原地写入」（cat > file），不能 mv/rm：
#     它是单文件 bind mount，替换 inode 会让容器内 /etc/caddy/Caddyfile 失联
#   - 绝不触碰 new-api 容器与 fnOS 的 443
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

COMPOSE_FILE="$PROJECT_DIR/docker-compose.yml"
ENV_FILE="$PROJECT_DIR/.env"
CANDIDATE_CADDYFILE="$PROJECT_DIR/Caddyfile"

CADDY_CONTAINER="upsnap-caddy"
CADDY_LIVE="/vol4/docker/upsnap/Caddyfile"      # 容器内挂载源（bind mount，须原地写）
CADDY_IN_CONTAINER="/etc/caddy/Caddyfile"
BACKUP_PREFIX="Caddyfile.bak-"

SERVICE="ai-relay-panel"
BFF_LOCAL="http://127.0.0.1:18080/panel-api/system/health"
BFF_PUBLIC="https://api.91666.icu:8443/panel-api/system/health"
NEWAPI_LOCAL="http://127.0.0.1:3000/api/status"
UPSNAP_PUBLIC="https://127.0.0.1:8090/"

TS="$(date +%Y%m%d-%H%M%S)"

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
c_red()   { printf '\033[31m%s\033[0m\n' "$*"; }
c_green() { printf '\033[32m%s\033[0m\n' "$*"; }
c_blue()  { printf '\033[36m%s\033[0m\n' "$*"; }
step() { echo; c_blue "==> $*"; }
die()  { c_red "✗ $*"; exit 1; }

# 兼容 v1/v2 compose 命令
compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose -f "$COMPOSE_FILE" "$@"
  else
    docker-compose -f "$COMPOSE_FILE" "$@"
  fi
}

require_env() {
  [ -f "$ENV_FILE" ] || die "缺少 $ENV_FILE。请先: cp .env.example .env 并填入真实凭证。"
  grep -q '^PANEL_SESSION_SECRET=' "$ENV_FILE" || die ".env 缺少 PANEL_SESSION_SECRET"
  if grep -q 'changeme\|change-this-to-a-random-string' "$ENV_FILE"; then
    die ".env 仍含占位值(changeme)，请填入真实凭证。"
  fi
}

wait_bff_local() {
  local timeout="${1:-90}" i=0
  step "等待 BFF 本地健康检查 ($BFF_LOCAL，最多 ${timeout}s)"
  while [ "$i" -lt "$timeout" ]; do
    if curl -fsS --max-time 3 "$BFF_LOCAL" 2>/dev/null | grep -q '"bff":"ok"'; then
      c_green "✓ BFF 本地健康：$(curl -fsS --max-time 3 "$BFF_LOCAL")"
      return 0
    fi
    sleep 2; i=$((i+2))
    printf '.'
  done
  echo
  c_red "✗ BFF 未在 ${timeout}s 内就绪，最近 40 行日志："
  compose logs --tail 40 "$SERVICE" || true
  return 1
}

# 校验候选 Caddyfile：拷进容器临时目录后 caddy validate（不绑定端口、不影响线上）
validate_candidate() {
  step "校验候选 Caddyfile（docker exec caddy validate）"
  docker cp "$CANDIDATE_CADDYFILE" "$CADDY_CONTAINER:/tmp/Caddyfile.candidate" >/dev/null
  if docker exec "$CADDY_CONTAINER" caddy validate --adapter caddyfile --config /tmp/Caddyfile.candidate; then
    c_green "✓ 候选配置语法通过"
  else
    die "候选 Caddyfile 校验失败，已中止（线上配置未改动）"
  fi
}

# 备份现网 Caddyfile（原地写之前）
backup_live_caddy() {
  [ -f "$CADDY_LIVE" ] || die "现网 $CADDY_LIVE 不存在，拒绝继续"
  local dst="/vol4/docker/upsnap/${BACKUP_PREFIX}${TS}"
  cp -p "$CADDY_LIVE" "$dst"
  c_green "✓ 已备份现网配置 → $dst"
}

# 原地写入（不破坏 bind mount）
install_caddy() {
  step "原地写入新 Caddyfile 并热加载"
  cat "$CANDIDATE_CADDYFILE" > "$CADDY_LIVE"
  c_green "✓ 已写入 $CADDY_LIVE"
  docker exec "$CADDY_CONTAINER" caddy reload --config "$CADDY_IN_CONTAINER" --force
  c_green "✓ caddy reload --force 完成"
}

verify_public() {
  step "端到端校验：$BFF_PUBLIC"
  local body code
  body="$(curl -ksS -o /tmp/_pub.json -w '%{http_code}' --max-time 10 "$BFF_PUBLIC" || true)"
  code="$body"; body="$(cat /tmp/_pub.json 2>/dev/null || true)"
  if [ "$code" = "200" ] && echo "$body" | grep -q '"bff":"ok"'; then
    c_green "✓ 8443 已指向 BFF：$body"
  else
    c_red "✗ 8443 未返回 BFF 响应（code=$code body=$body）"
    return 1
  fi
  step "校验应急后路：https://api.91666.icu:18083/api/status"
  code="$(curl -ksS -o /dev/null -w '%{http_code}' --max-time 10 https://api.91666.icu:18083/api/status || true)"
  [ "$code" = "200" ] && c_green "✓ 18083 原生面板可达 (200)" || c_red "✗ 18083 原生面板异常 (code=$code)"
  step "校验 UpSnap 未受影响：$UPSNAP_PUBLIC"
  code="$(curl -ksS -o /dev/null -w '%{http_code}' --max-time 10 "$UPSNAP_PUBLIC" || true)"
  [ "$code" = "200" ] && c_green "✓ UpSnap 8090 正常 (200)" || c_red "✗ UpSnap 8090 异常 (code=$code)"
  step "校验 New API 未受影响：$NEWAPI_LOCAL"
  code="$(curl -fsS -o /dev/null -w '%{http_code}' --max-time 10 "$NEWAPI_LOCAL" || true)"
  [ "$code" = "200" ] && c_green "✓ New API 3000 正常 (200)" || c_red "✗ New API 3000 异常 (code=$code)"
}

# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
cmd_deploy() {
  require_env

  # token 持久化目录：容器内 panel 用户(uid 10001)可写（docker-compose.yml 挂载 ./data:/data）
  step "准备 token 持久化目录（./data → 容器 /data，chown 10001）"
  mkdir -p "$PROJECT_DIR/data"
  chown 10001:10001 "$PROJECT_DIR/data" 2>/dev/null || c_red "⚠ chown 失败（需 root？）；若容器内写不了，token 持久化会自动降级"

  step "构建镜像（docker compose build）"
  compose build

  step "启动/更新容器（docker compose up -d）"
  compose up -d

  wait_bff_local 90

  validate_candidate      # ① 先 validate
  backup_live_caddy       # ② 再备份
  install_caddy           # ③ 写入 + reload --force

  verify_public || die "端到端校验未通过；可执行 ./deploy.sh rollback 回退 Caddy"

  echo
  c_green "======== 部署完成 ========"
  cmd_status
}

cmd_rollback() {
  local target="${1:-}"
  if [ -z "$target" ]; then
    target="$(ls -1t /vol4/docker/upsnap/${BACKUP_PREFIX}* 2>/dev/null | head -n1 || true)"
  fi
  [ -n "$target" ] && [ -f "$target" ] || die "找不到可用备份（$BACKUP_PREFIX*）。可手动指定：./deploy.sh rollback <备份文件>"

  step "回滚 Caddy：$target → $CADDY_LIVE"
  # 备份当前（回滚后仍可再次前进）
  cp -p "$CADDY_LIVE" "/vol4/docker/upsnap/${BACKUP_PREFIX}pre-rollback-${TS}" 2>/dev/null || true

  # 校验要恢复的配置（拷贝到候选位置后走同一校验逻辑）
  cp -p "$target" /tmp/_rollback_caddy
  docker cp /tmp/_rollback_caddy "$CADDY_CONTAINER:/tmp/Caddyfile.candidate" >/dev/null
  docker exec "$CADDY_CONTAINER" caddy validate --adapter caddyfile --config /tmp/Caddyfile.candidate \
    || die "备份文件校验失败，回滚中止（线上未改动）"

  cat "$target" > "$CADDY_LIVE"
  docker exec "$CADDY_CONTAINER" caddy reload --config "$CADDY_IN_CONTAINER" --force
  c_green "✓ 已恢复 $target 并热加载"

  step "校验原生面板入口"
  local code
  code="$(curl -ksS -o /dev/null -w '%{http_code}' --max-time 10 https://api.91666.icu:8443/api/status || true)"
  [ "$code" = "200" ] && c_green "✓ 8443 已回到 New API 原生面板 (200)" || c_red "✗ 8443 校验异常 (code=$code)"
  code="$(curl -ksS -o /dev/null -w '%{http_code}' --max-time 10 "$UPSNAP_PUBLIC" || true)"
  [ "$code" = "200" ] && c_green "✓ UpSnap 8090 正常 (200)" || c_red "✗ UpSnap 8090 异常 (code=$code)"
  echo
  c_blue "提示：如需再次切回新面板：./deploy.sh deploy"
}

cmd_status() {
  echo
  c_blue "---- 容器状态 ----"
  docker ps --filter "name=^${SERVICE}$" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' || true
  echo
  c_blue "---- 本地健康 127.0.0.1:18080 ----"
  curl -fsS --max-time 5 "$BFF_LOCAL" || c_red "(不可达)"
  echo; echo
  c_blue "---- 公网入口 https://api.91666.icu:8443 (期望 bff:ok) ----"
  curl -ksS --max-time 8 "$BFF_PUBLIC" || c_red "(不可达)"
  echo; echo
  c_blue "---- 应急后路 https://api.91666.icu:18083/api/status (期望 New API) ----"
  curl -ksS --max-time 8 -o /dev/null -w 'HTTP %{http_code}\n' https://api.91666.icu:18083/api/status || c_red "(不可达)"
  echo
  c_blue "---- UpSnap https://127.0.0.1:8090 (期望 200) ----"
  curl -ksS --max-time 8 -o /dev/null -w 'HTTP %{http_code}\n' "$UPSNAP_PUBLIC" || c_red "(不可达)"
  echo
  c_blue "---- New API本地 127.0.0.1:3000/api/status (期望 200) ----"
  curl -fsS --max-time 8 -o /dev/null -w 'HTTP %{http_code}\n' "$NEWAPI_LOCAL" || c_red "(不可达)"
  echo
  c_blue "---- Caddy 当前 :8443 反代目标 ----"
  grep -A2 ':8443' "$CADDY_LIVE" | grep -E 'reverse_proxy' || true
  echo
}

usage() {
  cat <<EOF
用法: $0 <命令>

  deploy              构建并部署，热加载 Caddy，端到端校验
  rollback [备份文件]  回滚 Caddy（默认恢复最新备份），验证站点可用
  status              查看容器与各入口健康

必读: docs/DEPLOY.md
EOF
}

case "${1:-}" in
  deploy)   cmd_deploy ;;
  rollback) cmd_rollback "${2:-}" ;;
  status)   cmd_status ;;
  *) usage; exit 1 ;;
esac
