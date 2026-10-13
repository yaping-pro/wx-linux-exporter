#!/usr/bin/env bash
set -euo pipefail

echo "=== wx-linux-exporter Self-Check ==="

VAULT_ROOT="${VAULT_ROOT:-$HOME/.local/share/wechat-vault}"
CONFIG_FILE="$VAULT_ROOT/config/export_service.json"

echo "1. Checking directory layout..."
if [[ -d "$VAULT_ROOT" ]]; then
    echo "  [OK] Vault root exists: $VAULT_ROOT"
else
    echo "  [WARN] Vault root not found: $VAULT_ROOT (run setup first)"
fi

echo "2. Checking config file..."
if [[ -f "$CONFIG_FILE" ]]; then
    echo "  [OK] Config file exists: $CONFIG_FILE"
    if grep -q "REPLACE_WITH_YOUR_AUTH_TOKEN" "$CONFIG_FILE" 2>/dev/null; then
        echo "  [WARN] Token still set to placeholder REPLACE_WITH_YOUR_AUTH_TOKEN"
    fi
else
    echo "  [WARN] Config file not found at $CONFIG_FILE"
fi

echo "3. Checking Python dependencies..."
python3 -c "import sqlite3, json, hashlib, argparse; print('  [OK] Standard library dependencies satisfied')"
python3 -c "import Crypto; print('  [OK] pycryptodome installed')" 2>/dev/null || echo "  [INFO] pycryptodome not installed (required for direct WAL decrypt if not using system wcdb)"

echo "4. Checking Decrypted Vault symlinks..."
if [[ -L "$VAULT_ROOT/decrypted/current" ]]; then
    target=$(readlink -f "$VAULT_ROOT/decrypted/current")
    echo "  [OK] current symlink -> $target"
else
    echo "  [INFO] No current decrypted version symlink yet (will be created on first sync)"
fi

echo "=== Self-check completed ==="
