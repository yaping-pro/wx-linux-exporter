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

echo "5. Service smoke test (loopback only, token required)..."
API_BIN="$(dirname "$0")/../bin/wechat_export_api.py"
if command -v python3 >/dev/null 2>&1 && [[ -f "$API_BIN" ]]; then
    SMOKE_PORT=18765
    SMOKE_TOKEN=$(openssl rand -hex 32 2>/dev/null || echo "selfcheck-insecure-token-not-openssl")
    SMOKE_HOST=127.0.0.1
    # Start the API on a scratch loopback port WITH a token.
    python3 "$API_BIN" --host "$SMOKE_HOST" --port "$SMOKE_PORT" --token "$SMOKE_TOKEN" \
        --vault-dir "$VAULT_ROOT/decrypted/current" >/tmp/wx_export_selfcheck.log 2>&1 &
    SMOKE_PID=$!
    # Wait for the server to come up (max ~5s).
    for _ in $(seq 1 50); do
        if curl -s -o /dev/null "http://$SMOKE_HOST:$SMOKE_PORT/health"; then
            break
        fi
        sleep 0.1
    done
    # /health must be unauthenticated (200) even with no token.
    HEALTH_NOAUTH=$(curl -s -o /dev/null -w "%{http_code}" "http://$SMOKE_HOST:$SMOKE_PORT/health")
    # /api/wechat/export without token must be rejected (401).
    EXPORT_NOAUTH=$(curl -s -o /dev/null -w "%{http_code}" "http://$SMOKE_HOST:$SMOKE_PORT/api/wechat/export")
    # /api/wechat/export with token must be served (200, even if vault is empty -> still 200 with empty payload).
    EXPORT_WITH_TOKEN=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $SMOKE_TOKEN" "http://$SMOKE_HOST:$SMOKE_PORT/api/wechat/export")
    kill "$SMOKE_PID" 2>/dev/null || true
    wait "$SMOKE_PID" 2>/dev/null || true
    if [[ "$HEALTH_NOAUTH" == "200" && "$EXPORT_NOAUTH" == "401" && "$EXPORT_WITH_TOKEN" == "200" ]]; then
        echo "  [OK] /health unauthenticated=200, export without token=401, export with token=200"
    else
        echo "  [FAIL] Smoke mismatch: health_noauth=$HEALTH_NOAUTH export_noauth=$EXPORT_NOAUTH export_token=$EXPORT_WITH_TOKEN"
        echo "  [FAIL] Server log:"; tail -n 20 /tmp/wx_export_selfcheck.log
    fi
else
    echo "  [INFO] python3 or api binary not available; skipping service smoke test"
fi


echo "6. Media enrichment layer (optional, off by default)..."
MEDIA_WORKER="$(dirname "$0")/../bin/wechat_media.py"
MEDIA_CLIENT="$(dirname "$0")/../bin/mineru_agent.py"
MEDIA_POLICY="$VAULT_ROOT/config/media_policy.json"
if [[ -f "$MEDIA_WORKER" && -f "$MEDIA_CLIENT" ]]; then
    echo "  [OK] worker + MinerU client present"
    python3 "$MEDIA_WORKER" --help >/dev/null 2>&1 && echo "  [OK] worker CLI loads" || echo "  [FAIL] worker CLI failed to load"
    python3 "$MEDIA_CLIENT" --help >/dev/null 2>&1 && echo "  [OK] MinerU client CLI loads" || echo "  [FAIL] MinerU client CLI failed to load"
    if [[ -f "$MEDIA_POLICY" ]]; then
        if python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get('enabled') else 1)" "$MEDIA_POLICY"; then
            echo "  [OK] media_policy.json present and enabled=true"
            python3 "$MEDIA_WORKER" --policy "$MEDIA_POLICY" --vault "$VAULT_ROOT" --dry-run >/dev/null 2>&1 \
                && echo "  [OK] dry-run completed" || echo "  [WARN] dry-run returned non-zero (check policy / vault paths)"
        else
            echo "  [INFO] media_policy.json present but enabled=false (layer is a no-op)"
        fi
    else
        echo "  [INFO] no media_policy.json -> layer disabled (cp config/media_policy.example.json to enable)"
    fi
    command -v curl >/dev/null 2>&1 && echo "  [OK] curl present (required for uploads)" || echo "  [WARN] curl not found (required for MinerU uploads)"
else
    echo "  [WARN] media enrichment components missing from bin/"
fi


echo "=== Self-check completed ==="
