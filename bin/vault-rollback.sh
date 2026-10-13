#!/usr/bin/env bash
set -euo pipefail
V="${VAULT_ROOT:-$HOME/.local/share/wechat-vault}"
DEC="$V/decrypted"
cur=$(readlink "$DEC/current" 2>/dev/null || echo "")
prev=$(readlink "$DEC/previous" 2>/dev/null || echo "")
[[ -n "${prev:-}" && -d "$prev" ]] || { echo "无可回滚版本 (No previous version found)"; exit 1; }
ln -sfn "$prev" "$DEC/.t.prev" && mv -T "$DEC/.t.prev" "$DEC/previous"
ln -sfn "$prev" "$DEC/.t.cur"  && mv -T "$DEC/.t.cur"  "$DEC/current"
echo "已回滚: $cur -> $prev"
