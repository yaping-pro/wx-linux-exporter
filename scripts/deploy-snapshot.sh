#!/usr/bin/env bash
#
# deploy-snapshot.sh — 快照式原子部署（替代已失效的 wechat-ops/deploy/deploy-vault.sh）
#
# 家服发布机制：~/.local/share/wechat-vault/bin 是指向 .deploy/<UTC时间戳>/ 的软链接，
# systemd unit 的 ExecStart 指向 .../bin/wechat_export_api.py（走软链接）。
# 因此升级 = 建新时间戳快照目录 + 原子切软链接 + 重启服务；回滚 = 切回旧快照。
#
# 设计要点：
#   - 所有远端执行集中在一个 rsh() 里，便于测试时把 ssh 替换成 mock。
#   - 任一步失败自动回滚（仅当已切换软链接时），由 ERR trap + 显式检查共同保证。
#   - 输出带 [STEP]/[OK]/[FAIL]/[ROLLBACK] 前缀，便于肉眼审计。
#
set -euo pipefail

# --------------------------------------------------------------------------- #
# 默认值（可被命令行参数覆盖）
# --------------------------------------------------------------------------- #
REMOTE="linux"
VAULT="${VAULT_ROOT:-}"          # 空 = 交给远端解析（见下方 resolve_remote_vault）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SOURCE="$REPO_ROOT/bin"
SERVICE="wechat-export-api"
DRY_RUN=0
NO_RESTART=0
ROLLBACK_TS=""
HEALTH_URL="http://127.0.0.1:8765/health"

# 回滚相关状态（在切换软链接后才会被设置）
SWITCHED=0
OLD_TARGET=""

# --------------------------------------------------------------------------- #
# 日志前缀
# --------------------------------------------------------------------------- #
log_step()  { printf '[STEP] %s\n'   "$*"; }
log_ok()    { printf '[OK] %s\n'      "$*"; }
log_fail()  { printf '[FAIL] %s\n'    "$*"; }
log_roll()  { printf '[ROLLBACK] %s\n' "$*"; }

# --------------------------------------------------------------------------- #
# 远端执行统一入口：所有 ssh 调用都走这里，测试时替换 ssh 即可 mock。
# 本地模式（REMOTE=localhost/127.0.0.1）直接 bash -c 执行，便于无 ssh 环境调试。
# --------------------------------------------------------------------------- #
rsh() {
  local cmd="$1"
  if [[ "$REMOTE" == "localhost" || "$REMOTE" == "127.0.0.1" ]]; then
    bash -c "$cmd"
  else
    # BatchMode=yes 避免在没有免密时被密码提示挂死；StrictHostKeyChecking=accept-new 仅首次接受指纹
    ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$REMOTE" "$cmd"
  fi
}

# --------------------------------------------------------------------------- #
# 用法 / --help
# --------------------------------------------------------------------------- #
usage() {
  cat <<'EOF'
deploy-snapshot.sh — 快照式原子部署 wx-linux-exporter 到远端 systemd 主机

用法:
  deploy-snapshot.sh [选项]
  deploy-snapshot.sh --rollback <TS> [选项]

选项:
  --remote <ssh-host>     远端 SSH 主机（默认: linux）
  --vault <远端路径>      远端 vault 根目录（默认: $HOME/.local/share/wechat-vault）
  --source <本地bin目录>  待发布的本地 bin 目录（默认: <repo>/bin）
  --service <systemd名>   systemd 服务单元名（默认: wechat-export-api）
  --dry-run               只打印将要执行的步骤，不做任何变更（rsync/ln/restart 均跳过）
  --no-restart            切完软链接后不重启服务（也不做健康检查断言）
  --rollback <时间戳>     回滚到指定 .deploy/<TS> 快照（只切换+重启+健康，不做 rsync）
  -h, --help              显示本帮助

前置条件:
  - 本机到远端 ssh 免密登录
  - 远端 sudo 免密 restart 该 systemd 服务（或当前用户有权 systemctl restart）
  - 本机安装 rsync

危险提示:
  - 不要运行旧的 ~/.claude/skills/wechat-ops/deploy/deploy-vault.sh，
    它会从没有媒体代码的 staging 目录覆盖生产，导致倒退。
EOF
}

# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
while [[ $# -gt 0 ]]; do
  case "$1" in
    --remote)     REMOTE="$2"; shift 2 ;;
    --vault)      VAULT="$2"; shift 2 ;;
    --source)     SOURCE="$2"; shift 2 ;;
    --service)    SERVICE="$2"; shift 2 ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --no-restart) NO_RESTART=1; shift ;;
    --rollback)   ROLLBACK_TS="$2"; shift 2 ;;
    -h|--help)    usage; exit 0 ;;
    *) log_fail "未知参数: $1"; usage; exit 2 ;;
  esac
done
# --------------------------------------------------------------------------- #
# 远端命令：失败即中止（静默失败是本次生产事故的同类问题）
#   rsh_or_die 用于读取类命令——远端连不上/命令报错必须直接失败，
#   绝不能用 `|| echo ""` 把错误伪装成"值为空"后继续往下走。
#   is-active 这类"非 0 也是正常语义"的探针用 rsh_probe 单独处理。
# --------------------------------------------------------------------------- #
rsh_or_die() {
  local out
  out="$(rsh "$1" 2>&1)"; local rc=$?
  if [[ $rc -ne 0 ]]; then
    log_fail "远端命令失败 (rc=$rc): $1"
    [[ -n "$out" ]] && log_fail "$out"
    exit 1
  fi
  printf '%s' "$out"
}

# 解析远端 vault 根目录（$HOME 必须在远端展开，绝不能在本机 macOS 先展开）
resolve_remote_vault() {
  if [[ -z "$VAULT" ]]; then
    # shellcheck disable=SC2016  # 故意单引号：让 $HOME/VAULT_ROOT 在远端展开
    VAULT="$(rsh_or_die 'printf %s "${VAULT_ROOT:-$HOME/.local/share/wechat-vault}"')"
    [[ -n "$VAULT" ]] || { log_fail "无法解析远端 vault 路径"; exit 1; }
    log_ok "vault=$VAULT (resolved on $REMOTE)"
  fi
}

# --------------------------------------------------------------------------- #
# 回滚：把 bin 软链接切回 $1 指定的快照目录，重启并做健康断言
# --------------------------------------------------------------------------- #
do_rollback() {
  local target="$1"
  trap - ERR   # 防止回滚过程中的失败再次触发 trap 导致递归
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log_roll "(dry-run) would: ln -sfn '$target' .bin.rb && mv -T .bin.rb bin"
    if [[ "$NO_RESTART" -ne 1 ]]; then
      log_roll "(dry-run) would: sudo -n systemctl restart '$SERVICE'"
      log_roll "(dry-run) would: curl -sf '$HEALTH_URL' (retry 5x)"
    fi
    log_ok "(dry-run) rollback plan printed; no changes made"
    return 0
  fi
  log_roll "reverting bin -> $target"
  if [[ -z "$target" ]]; then
    log_fail "没有可回滚的旧目标（首次部署且尚未成功过？），跳过软链接恢复"
  else
    rsh "ln -sfn '$target' '$VAULT/.bin.rb' && mv -T '$VAULT/.bin.rb' '$VAULT/bin'"
    log_ok "symlink restored -> $target"
  fi
  if [[ "$NO_RESTART" -ne 1 ]]; then
    rsh "sudo -n systemctl restart '$SERVICE'"
    sleep 2
    local active
    active="$(rsh "sudo -n systemctl is-active '$SERVICE'" 2>/dev/null || true)"
    if [[ "$active" != "active" ]]; then
      log_fail "回滚后服务仍非 active (got: $active)"
      return 1
    fi
    local ok=0
    for _ in 1 2 3 4 5; do
      if rsh "curl -sf '$HEALTH_URL'" >/dev/null 2>&1; then ok=1; break; fi
      sleep 1
    done
    if [[ "$ok" -ne 1 ]]; then
      log_fail "回滚后健康检查失败"
      return 1
    fi
  fi
  log_ok "rollback completed"
  return 0
}

# --------------------------------------------------------------------------- #
# ERR trap：任一步失败且已切换软链接时，自动回滚
# --------------------------------------------------------------------------- #
# shellcheck disable=SC2329  # invoked indirectly via `trap on_err ERR`
on_err() {
  local rc=$?
  if [[ "${SWITCHED:-0}" -eq 1 ]]; then
    log_fail "deploy aborted (rc=$rc) after symlink switch; auto-rolling back"
    do_rollback "$OLD_TARGET" || true
  else
    log_fail "deploy aborted (rc=$rc) before symlink switch; no rollback needed"
  fi
  exit "$rc"
}
trap 'on_err' ERR

# --------------------------------------------------------------------------- #
# 纯回滚分支
# --------------------------------------------------------------------------- #
if [[ -n "$ROLLBACK_TS" ]]; then
  resolve_remote_vault
  log_step "rollback requested -> .deploy/$ROLLBACK_TS"
  TARGET="$VAULT/.deploy/$ROLLBACK_TS"
  # 回滚前先记录基线（只读）
  BIN_BEFORE="$(rsh_or_die "readlink -f '$VAULT/bin'")"
  rsh "systemctl is-active '$SERVICE' 2>/dev/null" >/dev/null 2>&1 || true
  if do_rollback "$TARGET"; then
    log_ok "rollback to $ROLLBACK_TS succeeded"
    exit 0
  else
    log_fail "rollback to $ROLLBACK_TS failed"
    exit 1
  fi
fi

# 正常部署流程
# --------------------------------------------------------------------------- #
# 本地 commit（仅用于 CHANGELOG 记录，失败则记 unknown）
COMMIT="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"

# 0) 解析远端 vault 路径（必须在首次使用 $VAULT 之前）
resolve_remote_vault

# 1) 基线记录（只读）
log_step "recording baseline"
BIN_BEFORE="$(rsh_or_die "readlink -f '$VAULT/bin'")"
# is-active：rc=0 是 active，rc=3 是 inactive（都是"命令跑成功了"）；
# 其它 rc（ssh 断线、命令不存在）视为失败，必须中止。
ACTIVE_RC=0
ACTIVE_BEFORE="$(rsh "systemctl is-active '$SERVICE' 2>/dev/null")" || ACTIVE_RC=$?
if [[ $ACTIVE_RC -ne 0 && $ACTIVE_RC -ne 3 ]]; then
  log_fail "远端 systemctl is-active 执行失败 (rc=$ACTIVE_RC)"
  exit 1
fi
ACTIVE_BEFORE="${ACTIVE_BEFORE:-unknown}"
HTTP_BEFORE="$(rsh_or_die "curl -s -o /dev/null -w '%{http_code}' '$HEALTH_URL'")"
OLD_BASELINE_TS="$(basename "$BIN_BEFORE" 2>/dev/null || echo none)"
if [[ "$DRY_RUN" -ne 1 ]]; then
  # deploy.log 在 .deploy 下；快照目录要等步骤 2 才建，这里先确保父目录存在
  rsh "mkdir -p '$VAULT/.deploy'" || { log_fail "无法创建远端 .deploy 目录"; exit 1; }
  {
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') deploy start commit=$COMMIT from=$OLD_BASELINE_TS remote=$REMOTE service=$SERVICE"
    echo "  baseline: bin=$BIN_BEFORE active=$ACTIVE_BEFORE health=$HTTP_BEFORE"
  } | rsh_or_die "cat >> '$VAULT/.deploy/deploy.log'"
fi
log_ok "baseline: bin=$BIN_BEFORE active=$ACTIVE_BEFORE health=$HTTP_BEFORE"

# 2) 计算时间戳 + 建快照目录
TS="$(date -u '+%Y%m%dT%H%M%SZ')"
log_step "create snapshot dir .deploy/$TS"
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[STEP] (dry-run) would: mkdir -p $VAULT/.deploy/$TS"
else
  rsh "mkdir -p '$VAULT/.deploy/$TS'"
  log_ok "snapshot dir ready"
fi

# 3) rsync 代码（仅 .py / .sh，排除缓存）
log_step "rsync $SOURCE/ -> $REMOTE:$VAULT/.deploy/$TS/"
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[STEP] (dry-run) would: rsync -az --include='*/' --include='*.py' --include='*.sh' --exclude='__pycache__/' --exclude='*.pyc' --exclude='*.egg-info/' $SOURCE/ $REMOTE:$VAULT/.deploy/$TS/"
else
  rsync -az --include='*/' --include='*.py' --include='*.sh' \
    --exclude='__pycache__/' --exclude='*.pyc' --exclude='*.egg-info/' \
    "$SOURCE/" "$REMOTE:$VAULT/.deploy/$TS/"
  log_ok "rsync done"
fi

# 4) 远端自检：py_compile + --help
log_step "self-check on remote (py_compile + --help)"
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[STEP] (dry-run) would: python3 -m py_compile $VAULT/.deploy/$TS/*.py"
  echo "[STEP] (dry-run) would: python3 $VAULT/.deploy/$TS/wechat_export_api.py --help"
else
  rsh "cd '$VAULT/.deploy/$TS' && python3 -m py_compile *.py"
  log_ok "py_compile passed"
  if ! rsh "timeout 20 python3 '$VAULT/.deploy/$TS/wechat_export_api.py' --help" >/dev/null 2>&1; then
    log_fail "wechat_export_api.py --help 执行失败（自检未通过）"
    exit 1
  fi
  log_ok "entrypoint --help OK"
fi

# 5) 原子切换软链接：ln -sfn 到临时名 + mv -T 覆盖（mv -T 保证原子性）
log_step "atomic switch: bin -> .deploy/$TS"
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[STEP] (dry-run) would: ln -sfn .deploy/$TS $VAULT/.bin.tmp && mv -T $VAULT/.bin.tmp $VAULT/bin"
else
  rsh "ln -sfn '.deploy/$TS' '$VAULT/.bin.tmp' && mv -T '$VAULT/.bin.tmp' '$VAULT/bin'"
  SWITCHED=1
  OLD_TARGET="$BIN_BEFORE"
  log_ok "symlink switched -> .deploy/$TS"
fi

# 6) 重启 + is-active 断言
if [[ "$NO_RESTART" -eq 1 ]]; then
  log_step "--no-restart: 跳过重启与健康检查"
else
  log_step "restart service (sudo -n systemctl restart $SERVICE)"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[STEP] (dry-run) would: sudo -n systemctl restart $SERVICE"
  else
    rsh "sudo -n systemctl restart '$SERVICE'"
    sleep 2
    ACTIVE_NOW="$(rsh "sudo -n systemctl is-active '$SERVICE'" 2>/dev/null || true)"
    if [[ "$ACTIVE_NOW" != "active" ]]; then
      log_fail "重启后服务非 active (got: $ACTIVE_NOW)"
      do_rollback "$OLD_TARGET"
      exit 1
    fi
    log_ok "service active"
  fi

  # 7) 健康断言（重试 5 次 × 1s）
  log_step "health assert $HEALTH_URL"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[STEP] (dry-run) would: curl -sf $HEALTH_URL (retry 5x)"
  else
    HEALTH_OK=0
    for _ in 1 2 3 4 5; do
      if rsh "curl -sf '$HEALTH_URL'" >/dev/null 2>&1; then HEALTH_OK=1; break; fi
      sleep 1
    done
    if [[ "$HEALTH_OK" -ne 1 ]]; then
      log_fail "健康检查失败（5 次重试后仍不可达）"
      do_rollback "$OLD_TARGET"
      exit 1
    fi
    log_ok "health OK"
  fi
fi

# 8) 写本地 CHANGELOG（远端追加）
if [[ "$DRY_RUN" -ne 1 ]]; then
  ROLLBACK_CMD="$0 --rollback $TS --remote $REMOTE --vault $VAULT --service $SERVICE"
  {
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') ts=$TS commit=$COMMIT from=$OLD_BASELINE_TS -> $TS | rollback: $ROLLBACK_CMD"
  } | rsh "cat >> '$VAULT/.deploy/CHANGELOG.local'" 2>/dev/null || true
  log_ok "CHANGELOG.local updated"
fi

log_ok "deploy $TS completed (from=$OLD_BASELINE_TS)"
exit 0
