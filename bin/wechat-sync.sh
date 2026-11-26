#!/usr/bin/env bash
set -euo pipefail

VAULT_ROOT="${VAULT_ROOT:-$HOME/.local/share/wechat-vault}"
DEC_DIR="$VAULT_ROOT/decrypted/current"
KEYS_DIR="$VAULT_ROOT/keys"
KEYS_FILE="$KEYS_DIR/all_keys.json"
# 密钥工具走 `<tool> extract --db-dir <dir> --output <file>` 契约（wcdb-key-tool 家族）。
# 注意：仓库自带的 bin/extract_keys.py 是 macOS frida 工具（CLI 为 --targets/--db-base），
# 不支持该契约；把它当 KEY_TOOL 会让每轮同步在 argparse 处失败（2026-11-26 linux1 实测）。
KEY_TOOL="${KEY_TOOL:-}"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
PASSPHRASE_FILE="${PASSPHRASE_FILE:-$HOME/.wcdb-key-tool/wechat-passphrase.json}"
LOG_FILE="$VAULT_ROOT/sync.log"
RETENTION_VERSIONS="${RETENTION_VERSIONS:-${1:-3}}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# stderr + log file, keeps stdout clean for command substitution
log_err() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE" >&2
}

# A tool qualifies only if its --help advertises an `extract` subcommand.
# `--help` alone cannot probe this: argparse exits 0 on `extract --help` even
# when no such subcommand exists, so the help text is inspected instead.
key_tool_supports_extract() {
    local tool="$1" help
    [[ -f "$tool" ]] || return 1
    help="$("$PYTHON_BIN" "$tool" --help 2>/dev/null)" || return 1
    grep -qE '(^|[[:space:]{,])extract([[:space:]},]|$)' <<<"$help"
}

resolve_key_tool() {
    local cand
    for cand in "${KEY_TOOL:-}" \
        "/opt/wcdb-key-tool/wcdb_key_tool.py" \
        "$VAULT_ROOT/bin/wcdb_key_tool.py"; do
        [[ -n "$cand" && -f "$cand" ]] || continue
        if key_tool_supports_extract "$cand"; then
            printf '%s\n' "$cand"
            return 0
        fi
        log_err "[WARN] 密钥工具不满足 \`extract --db-dir --output\` 契约，已跳过: $cand"
    done
    return 1
}

mkdir -p "$VAULT_ROOT/bin" "$KEYS_DIR" "$VAULT_ROOT/decrypted" "$VAULT_ROOT/exports"

# 1. 检查微信是否运行
if ! pgrep -x wechat >/dev/null 2>&1; then
    log "[WARN] 微信主进程未运行，跳过本次同步。"
    exit 0
fi

# 2. 检查是否有数据库文件存在
DB_DIR=""
for cand in \
    "$HOME/Documents/xwechat_files"/*/db_storage \
    "$HOME/.local/share/com.tencent.wechat/xwechat_files"/*/db_storage \
    "$HOME/.xwechat"/*/db_storage \
    "$HOME/xwechat_files"/*/db_storage; do
    if compgen -G "$cand" > /dev/null; then
        for d in $cand; do
            if [[ -d "$d" ]] && find "$d" -maxdepth 3 -name "*.db" | grep -q .; then
                DB_DIR="$d"
                break 2
            fi
        done
    fi
done

if [[ -z "$DB_DIR" ]]; then
    log "[INFO] 尚未检测到微信数据库文件（微信可能尚未登录），待扫码登录后自动处理。"
    exit 0
fi

log "[INFO] 检测到微信数据库源目录: $DB_DIR"

# 3. 提取/验证密钥
KEY_TOOL="$(resolve_key_tool || true)"
if [[ -z "$KEY_TOOL" ]]; then
    log_err "[WARN] 未找到满足 \`extract --db-dir --output\` 契约的密钥工具" \
            "（候选：\$KEY_TOOL 环境变量、/opt/wcdb-key-tool/wcdb_key_tool.py、\$VAULT_ROOT/bin/wcdb_key_tool.py）"
else
    log "[INFO] 密钥工具: $KEY_TOOL"
fi

if [[ -f "$KEYS_FILE" ]]; then
    log "[INFO] 验证并使用已有密钥文件: $KEYS_FILE"
    if [[ -n "$KEY_TOOL" ]]; then
        sudo "$PYTHON_BIN" "$KEY_TOOL" extract --db-dir "$DB_DIR" --output "$KEYS_FILE" \
            >> "$LOG_FILE" 2>&1 \
            || log_err "[WARN] 密钥刷新失败 rc=$?，沿用已有 all_keys.json 继续发布；" \
                       "若微信升级/重装导致密钥失效，请人工重跑密钥提取"
    else
        log_err "[WARN] 无可用密钥工具，本轮不刷新密钥，沿用已有 $KEYS_FILE"
    fi
elif [[ -z "$KEY_TOOL" ]]; then
    log_err "[WARN] 无密钥文件且无可用密钥工具，本轮跳过发布。"
elif [[ -f "$PASSPHRASE_FILE" ]]; then
    log "[INFO] 发现 Passphrase 缓存，正在派生密钥..."
    sudo "$PYTHON_BIN" "$KEY_TOOL" extract --db-dir "$DB_DIR" --output "$KEYS_FILE" >> "$LOG_FILE" 2>&1
else
    log "[INFO] 未检测到 Passphrase，正在附加 GDB 捕获..."
    sudo "$PYTHON_BIN" "$KEY_TOOL" extract --db-dir "$DB_DIR" --output "$KEYS_FILE" --timeout 60 >> "$LOG_FILE" 2>&1
fi

# 4. 原子发布（解密到新版本目录 -> 合并 WAL -> 切换符号链接）
if [[ -f "$KEYS_FILE" ]]; then
    log "[INFO] 开始原子发布微信数据库版本 ..."
    python3 "$VAULT_ROOT/bin/wechat_publish.py" \
        --db-dir "$DB_DIR" \
        --vault-root "$VAULT_ROOT" \
        --keys-file "$KEYS_FILE" \
        --keep-versions "${RETENTION_VERSIONS:-3}" >> "$LOG_FILE" 2>&1 && rc=0 || rc=$?
    if [[ $rc -ne 0 ]]; then
        log "[ERROR] 发布失败 rc=$rc，已保留上一版本（current 未切换）。"
        exit "$rc"
    fi
fi

# 5. 权限规范化 (XDG 与安全规范)
chmod -R 700 "$VAULT_ROOT"
find -L "$DEC_DIR" -type f -exec chmod 600 {} + 2>/dev/null || true
if [[ -f "$KEYS_FILE" ]]; then
    chmod 600 "$KEYS_FILE"
fi

log "[INFO] 微信数据库同步流水线执行完成。"
