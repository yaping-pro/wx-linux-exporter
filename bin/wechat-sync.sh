#!/usr/bin/env bash
set -euo pipefail

VAULT_ROOT="${VAULT_ROOT:-$HOME/.local/share/wechat-vault}"
DEC_DIR="$VAULT_ROOT/decrypted/current"
KEYS_DIR="$VAULT_ROOT/keys"
KEYS_FILE="$KEYS_DIR/all_keys.json"
KEY_TOOL="${KEY_TOOL:-/opt/wcdb-key-tool/wcdb_key_tool.py}"
PASSPHRASE_FILE="${PASSPHRASE_FILE:-$HOME/.wcdb-key-tool/wechat-passphrase.json}"
LOG_FILE="$VAULT_ROOT/sync.log"
RETENTION_VERSIONS="${RETENTION_VERSIONS:-${1:-3}}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
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
if [[ -f "$KEYS_FILE" ]]; then
    log "[INFO] 验证并使用已有密钥文件: $KEYS_FILE"
    sudo /usr/bin/python3 "$KEY_TOOL" extract --db-dir "$DB_DIR" --output "$KEYS_FILE" >> "$LOG_FILE" 2>&1 || true
elif [[ -f "$PASSPHRASE_FILE" ]]; then
    log "[INFO] 发现 Passphrase 缓存，正在派生密钥..."
    sudo /usr/bin/python3 "$KEY_TOOL" extract --db-dir "$DB_DIR" --output "$KEYS_FILE" >> "$LOG_FILE" 2>&1
else
    log "[INFO] 未检测到 Passphrase，正在附加 GDB 捕获..."
    sudo /usr/bin/python3 "$KEY_TOOL" extract --db-dir "$DB_DIR" --output "$KEYS_FILE" --timeout 60 >> "$LOG_FILE" 2>&1
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
