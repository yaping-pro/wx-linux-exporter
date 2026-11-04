#!/system/bin/sh
# sfa_watchdog.sh — redmi-wxpad Tailnet 出口隧道守护（Magisk /data/adb/service.d）
#
# 背景: 导出设备的 tailnet 出口由 SFA (io.nekohasekai.sfa, sing-box) 承载。
# SFA 一死, tun0 被拆, 设备失去 100.80.174.96, 家服青龙经
# http://100.80.174.96:8765 的拉取即超时。
#
# 设计要点:
#   1. 解锁门禁: /data 处于 FBE 凭据加密区, 用户解锁前 SFA 打开 Room/SQLite 必然
#      抛 SQLITE_CANTOPEN。2026-11-04 实证: 开机即盲拉 → 7 连崩 → Android 放弃
#      自动重启, 而本脚本 600s 退避又把"用户已解锁"这件事晾了 6 分钟。
#      因此启动前必须等到 am get-started-user-state 0 = RUNNING_UNLOCKED。
#   2. 静默拉起: 只用 am start-foreground-service -n .bg.VPNService,
#      严禁 monkey / am start / uiautomator, 绝不抢占前台 UI。
#   3. 判活口径: 进程 (pidof) 与隧道 (sysfs operstate) 分开判,
#      "进程活但隧道死" 必须触发自愈 (2026-10-23 卡死类)。
#   4. 退避有界: 上限 30s。600s 的退避会把可自愈故障放大成人工介入。
#   5. 单例锁 + 日志轮转 + Binder DAC 规避 (>/dev/null 2>&1)
#   6. .bak 绝不放 /data/adb/service.d/ (Magisk 会执行该目录所有 .sh)。
#
# 所有路径/节拍可由环境变量覆盖，仅用于测试与迁移，生产用默认值。

SFA_PKG="${SFA_PKG:-io.nekohasekai.sfa}"
SFA_COMPONENT="${SFA_COMPONENT:-${SFA_PKG}/.bg.VPNService}"
SFA_TUN_PATH="${SFA_TUN_PATH:-/sys/class/net/tun0}"
SFA_PID_FILE="${SFA_PID_FILE:-/data/local/tmp/sfa_watchdog.pid}"
SFA_LOG_FILE="${SFA_LOG_FILE:-/data/local/tmp/sfa_watchdog.log}"
SFA_TAILNET_TS_FILE="${SFA_TAILNET_TS_FILE:-/data/local/tmp/sfa_tailnet_restart.ts}"

MAX_LOG_LINES="${SFA_MAX_LOG_LINES:-200}"
CHECK_INTERVAL="${SFA_CHECK_INTERVAL:-30}"
START_DELAY="${SFA_START_DELAY:-10}"
UNLOCK_POLL="${SFA_UNLOCK_POLL:-3}"
UNLOCK_MAX_POLLS="${SFA_UNLOCK_MAX_POLLS:-200}"     # 约 10 分钟后 fail-open
START_TIMEOUT="${SFA_START_TIMEOUT:-15}"
BACKOFF_BASE="${SFA_BACKOFF_BASE:-30}"
BACKOFF_MAX="${SFA_BACKOFF_MAX:-30}"
BACKOFF_FAIL_THRESHOLD="${SFA_BACKOFF_FAIL_THRESHOLD:-3}"
TAILNET_PEER="${SFA_TAILNET_PEER:-100.67.218.81}"
TAILNET_FAIL_THRESHOLD="${SFA_TAILNET_FAIL_THRESHOLD:-5}"
TAILNET_RESTART_COOLDOWN="${SFA_TAILNET_RESTART_COOLDOWN:-600}"
MAX_LOOPS="${SFA_MAX_LOOPS:-0}"                     # 0 = 无限循环 (测试seam)

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [sfa_watchdog] $1" >> "$SFA_LOG_FILE" 2>/dev/null
    if [ -f "$SFA_LOG_FILE" ]; then
        LINES=$(wc -l < "$SFA_LOG_FILE" 2>/dev/null || echo 0)
        if [ "$LINES" -gt "$MAX_LOG_LINES" ]; then
            tail -n "$MAX_LOG_LINES" "$SFA_LOG_FILE" > "${SFA_LOG_FILE}.tmp" 2>/dev/null \
                && mv "${SFA_LOG_FILE}.tmp" "$SFA_LOG_FILE"
        fi
    fi
}

# ---- 单例治理 ----
mkdir -p "$(dirname "$SFA_PID_FILE")" 2>/dev/null
for old_p in $(pgrep -f "sfa_watchdog\.sh" 2>/dev/null); do
    if [ "$old_p" != "$$" ] && [ "$old_p" != "$PPID" ]; then
        kill -9 "$old_p" 2>/dev/null || true
    fi
done
echo "$$" > "$SFA_PID_FILE" 2>/dev/null

sfa_alive() {
    pidof "$SFA_PKG" >/dev/null 2>&1
}

tun_ok() {
    [ -f "$SFA_TUN_PATH/operstate" ] \
        && [ "$(cat "$SFA_TUN_PATH/operstate" 2>/dev/null)" != "down" ]
}

wlan_up() {
    ip addr show wlan0 2>/dev/null | grep -q "inet "
}

tailnet_ok() {
    ping -c 1 -W 2 "$TAILNET_PEER" >/dev/null 2>&1
}

# 拉起 VPNService 并等到 tun0 真正出现; 返回 0 表示隧道已起
start_tunnel() {
    waited=0
    am start-foreground-service -n "$SFA_COMPONENT" >/dev/null 2>&1
    while [ "$waited" -lt "$START_TIMEOUT" ]; do
        sleep 1
        waited=$((waited + 1))
        if tun_ok; then
            log "start_tunnel: tun0 up after ${waited}s"
            return 0
        fi
    done
    log "start_tunnel: tun0 not up after ${START_TIMEOUT}s"
    return 1
}

# 卡死自愈: 重启 SFA 使 tailscale 端点重新注册 (冷却中不动作)
restart_sfa_tailnet() {
    now=$(date +%s)
    last=$(cat "$SFA_TAILNET_TS_FILE" 2>/dev/null || echo 0)
    if [ $((now - last)) -lt "$TAILNET_RESTART_COOLDOWN" ]; then
        log "tailnet stuck but restart cooling down ($((now - last))s < ${TAILNET_RESTART_COOLDOWN}s)"
        return 1
    fi
    log "tailnet stuck with wlan0 up -> force-stop + restart SFA"
    echo "$now" > "$SFA_TAILNET_TS_FILE" 2>/dev/null
    am force-stop "$SFA_PKG" >/dev/null 2>&1
    sleep 2
    start_tunnel
}

# FBE 门禁: 用户解锁前不动 SFA。判据不可用时 (老系统无此命令) fail-open。
wait_for_user_unlocked() {
    polls=0
    while :; do
        state=$(am get-started-user-state 0 2>/dev/null)
        if [ -z "$state" ]; then
            log "unlock state unavailable, proceeding (fail-open)"
            return 0
        fi
        if [ "$state" = "RUNNING_UNLOCKED" ]; then
            log "user 0 unlocked (after ${polls} poll(s))"
            return 0
        fi
        polls=$((polls + 1))
        if [ "$polls" -ge "$UNLOCK_MAX_POLLS" ]; then
            log "still ${state} after ${polls} polls, proceeding (fail-open)"
            return 0
        fi
        sleep "$UNLOCK_POLL"
    done
}

log "sfa_watchdog started (PID: $$)"

until [ "$(getprop sys.boot_completed 2>/dev/null)" = "1" ]; do
    sleep 2
done
sleep "$START_DELAY"
log "boot completed, waiting for user 0 unlock before touching SFA"
wait_for_user_unlocked
log "entering guard loop"

consecutive_fail=0
backoff=0
tailnet_fail=0
loops=0

while :; do
    if [ "$backoff" -gt 0 ]; then
        sleep "$backoff"
    else
        sleep "$CHECK_INTERVAL"
    fi

    if sfa_alive; then
        if tun_ok; then
            if [ "$consecutive_fail" -ne 0 ] || [ "$backoff" -ne 0 ]; then
                log "SFA healthy and tun0 up, reset backoff"
            fi
            consecutive_fail=0
            backoff=0

            # 隧道活着 != tailnet 可达
            if tailnet_ok; then
                if [ "$tailnet_fail" -ne 0 ]; then
                    log "tailnet recovered after ${tailnet_fail} failed probe(s)"
                    tailnet_fail=0
                fi
            else
                tailnet_fail=$((tailnet_fail + 1))
                log "tailnet probe failed (${tailnet_fail}/${TAILNET_FAIL_THRESHOLD})"
                if [ "$tailnet_fail" -ge "$TAILNET_FAIL_THRESHOLD" ]; then
                    if wlan_up; then
                        restart_sfa_tailnet && tailnet_fail=0
                    else
                        log "wlan0 has no IPv4, skip SFA restart (uplink down)"
                        tailnet_fail=0
                    fi
                fi
            fi
        else
            # 进程在但 tun0 没起来 -> 自愈隧道, 不 kill 进程
            if start_tunnel; then
                log "SFA tunnel restored, tun0 up"
                consecutive_fail=0
                backoff=0
            else
                consecutive_fail=$((consecutive_fail + 1))
                if [ "$consecutive_fail" -ge "$BACKOFF_FAIL_THRESHOLD" ]; then
                    backoff=$((backoff <= 0 ? BACKOFF_BASE : backoff * 2))
                    [ "$backoff" -gt "$BACKOFF_MAX" ] && backoff="$BACKOFF_MAX"
                    log "tunnel-restore failed ${consecutive_fail} times, backoff ${backoff}s"
                fi
            fi
        fi
    else
        # 进程不在 -> 拉起
        if start_tunnel; then
            log "SFA launched and tun0 up"
            consecutive_fail=0
            backoff=0
        else
            consecutive_fail=$((consecutive_fail + 1))
            if [ "$consecutive_fail" -ge "$BACKOFF_FAIL_THRESHOLD" ]; then
                backoff=$((backoff <= 0 ? BACKOFF_BASE : backoff * 2))
                [ "$backoff" -gt "$BACKOFF_MAX" ] && backoff="$BACKOFF_MAX"
                log "relaunch failed ${consecutive_fail} times, backoff ${backoff}s"
            fi
        fi
    fi

    if [ "$MAX_LOOPS" -gt 0 ]; then
        loops=$((loops + 1))
        [ "$loops" -ge "$MAX_LOOPS" ] && break
    fi
done

log "guard loop exited (test seam MAX_LOOPS=${MAX_LOOPS})"
exit 0