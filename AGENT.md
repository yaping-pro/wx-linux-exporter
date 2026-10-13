# Autonomous Agent Runbook (`AGENT.md`)

[中文](#中文) | [English](#english)

---

## 中文

> **面向 AI 智能体（Pi / omp / Claude Code / Codex / Cursor）的无交互自主部署运行手册**  
> 本手册为执行全自动化部署的智能体定义了不可违反的硬门禁、有序执行步骤、幂等性保障、安全红线与回滚策略。

### 1. 硬性门禁与环境要求 (Hard Gates)

在执行任何变更前，必须依次验证以下前提条件。任一条件不满足，**必须立即中止（ABORT）并向用户报告缺失项**，不得擅自猜测或越权修改。

- [ ] **Gate 1: 系统架构与 OS**
  - 要求：Linux x86_64 / arm64，推荐 Ubuntu 22.04+ 或 Debian 12+。
  - 验证命令：`uname -s` 输出必须为 `Linux`。
- [ ] **Gate 2: 部署环境类型（极其重要）**
  - **本项目代码提取自一套已在家庭服务器（Homelab）上连续、长效运行的真实生产流水线（已稳定运行数周，即当前向你 AI 助理持续供数的那套系统），发布版已对个人标识脱敏。**
  - **唯一未经充分验证的维度是「部署到远程 VPS / 不同发行版」**：代码逻辑本身已在生产环境被持续验证。若检测到目标环境为纯 VPS 或云服务器，Agent 仍须显式向用户发出警示，告知其关注：
    - 无 X11 显示环境时需依赖 `Xvfb`。
    - 大容量 WCDB 解密需要足够的可用内存（建议 >= 4GB）与磁盘 I/O。
    - 端口 `8765` 严禁直接暴露在公网（仅限 `127.0.0.1` 或 Tailscale 私网）。
    - 云厂商无 adb/物理设备环境。
- [ ] **Gate 3: 依赖组件**
  - `python3` (>= 3.10) 与 `sqlite3`、`gdb`、`Xvfb`。
  - 可用性检查：`which python3 gdb Xvfb systemctl`。
- [ ] **Gate 4: 微信运行与数据库存在**
  - 检查是否存在 `/opt/wechat/wechat` 或系统中已安装原生 Linux 微信客户端。
  - 检查 `$HOME/Documents/xwechat_files` 或 `$HOME/.local/share/com.tencent.wechat` 下是否已有用户登录生成的数据库文件。若微信尚未登录，则提示用户完成首次扫码登录后再启动定时解密。

---

### 2. 有序执行步骤 (Ordered Steps)

智能体部署流程分为 5 个有序阶段：

```
[Phase 1: 环境检查与依赖安装]
         ↓
[Phase 2: 目录结构与配置生成]
         ↓
[Phase 3: 脚本与工具链部署]
         ↓
[Phase 4: Systemd 服务单元编排]
         ↓
[Phase 5: 端到端健康检查与鉴权验收]
```

#### Phase 1: 依赖安装
```bash
sudo apt-get update && sudo apt-get install -y python3 python3-pip gdb xvfb
pip3 install pycryptodome zstandard
```

#### Phase 2: 创建目录与安全配置
```bash
VAULT_ROOT="$HOME/.local/share/wechat-vault"
mkdir -p "$VAULT_ROOT/bin" "$VAULT_ROOT/config" "$VAULT_ROOT/keys" "$VAULT_ROOT/decrypted/versions" "$VAULT_ROOT/state"
chmod 700 "$VAULT_ROOT"

# 生成 32 字节高熵鉴权 Token
TOKEN=$(openssl rand -hex 32)
cat <<EOF > "$VAULT_ROOT/config/export_service.json"
{
  "host": "127.0.0.1",
  "port": 8765,
  "token": "$TOKEN",
  "vault_dir": "$VAULT_ROOT/decrypted/current",
  "allowlist": [],
  "blocklist": []
}
EOF
chmod 600 "$VAULT_ROOT/config/export_service.json"
```

#### Phase 3: 安装发布与同步脚本
将仓库 `bin/` 下所有 Python 与 Shell 脚本部署至 `$VAULT_ROOT/bin/`：
```bash
cp bin/* "$VAULT_ROOT/bin/"
chmod +x "$VAULT_ROOT/bin/"*
```

#### Phase 4: 配置 Systemd 服务
以普通用户权限运行（或通过系统 systemd 并注入 User 用户名）。推荐使用仓库自带的一键脚本，它会复制单元、替换安装期占位符 `__WXUSER__`/`__WXHOME__`，并启用+启动服务；脚本会在占位符或 `%h`/`%I` 残留时中止，避免未渲染单元直达 systemd：
```bash
sudo ./deploy/install-systemd.sh
# 或指定目标用户/家目录：
# sudo ./deploy/install-systemd.sh --user wechat --home /home/wechat
```

手动等价步骤（脚本即执行这些）：
```bash
USER_NAME=$(whoami)
HOME_DIR="$HOME"
# 安装服务单元，渲染安装期占位符 (__WXUSER__ -> 当前用户, __WXHOME__ -> 真实家目录)。
# 这些单元是 SYSTEM 作用域，旧版 %h 会展开为 /root 导致启动失败。
sudo cp deploy/systemd/wechat-*.service /etc/systemd/system/
sudo cp deploy/systemd/wechat-*.timer /etc/systemd/system/
sudo sed -i "s|__WXUSER__|$USER_NAME|g; s|__WXHOME__|$HOME_DIR|g" /etc/systemd/system/wechat-*.service

sudo systemctl daemon-reload
sudo systemctl enable --now wechat-xvfb.service
sudo systemctl enable --now wechat-runner.service
sudo systemctl enable --now wechat-decrypt.timer
sudo systemctl enable --now wechat-export-api.service
```

#### Phase 5: 健康检查与负向鉴权测试
```bash
# 1. 负向鉴权：无 Token 访问必须返回 401
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8765/api/wechat/export | grep -q "401"

# 2. 正向健康检查：健康端点必须返回 200
curl -s http://127.0.0.1:8765/health | grep -q '"status":"ok"'

# 3. 携带 Token 查询
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/wechat/export | grep -q '"exported_at"'
```

---

### 3. 安全红线 (Safety Guardrails)

- 🔴 **严禁将服务端口 8765 监听在 `0.0.0.0`**（默认仅绑定 `127.0.0.1`；跨机调用只走 Tailscale / WireGuard 私网或 SSH 隧道）。
- 🔴 **严禁将任何解密数据库（`*.db`、`*.db-wal`）提交至 Git 或暴露在外网**。
- 🔴 **解密目录权限必须硬锁为 0700 / 0600**，确保多租户隔离。
- 🔴 **鉴权默认失败关闭（Fail-Closed）**：`wechat_export_api.py` **必须**配置 Bearer Token（config 的 `token` 字段或 `--token`）。未配置 token 且未显式传入 `--insecure-no-auth` 时，服务**拒绝启动**（退出码 2）。Token 缺失绝不会退化为“免密放行”。
- 🟡 **`/health` 存活探针免鉴权（显式设计）**：`GET /health` 与 `GET /api/wechat/health` **无需 Token** 即返回 `200`，因为它们仅暴露服务存活状态、不泄露任何聊天数据。所有承载聊天数据的端点（特别是 `/api/wechat/export`）**始终要求** `Authorization: Bearer <TOKEN>`，无 Token 一律 `401`。请勿在自检或告警探针中依赖导出端点的免鉴权行为。
---

### 4. 回滚与卸载 (Rollback & Uninstall)

若发生故障或需要完整卸载：
```bash
# 1. 停止并禁用所有 systemd 服务
sudo systemctl disable --now wechat-export-api.service wechat-decrypt.timer wechat-decrypt.service wechat-runner.service wechat-xvfb.service
sudo rm -f /etc/systemd/system/wechat-*
sudo systemctl daemon-reload

# 2. 清理临时解密数据
rm -rf "$HOME/.local/share/wechat-vault/decrypted"
# （可选）完全移除数据目录：rm -rf "$HOME/.local/share/wechat-vault"
```

---

## English

> **Autonomous Agent Runbook for AI Coding Assistants (Pi, omp, Claude Code, Codex, Cursor)**

### 1. Hard Gates & Verification

Before mutating the system, the agent MUST verify all prerequisites. If any check fails, **ABORT IMMEDIATELY** and report the blocker.

- [ ] **Gate 1: Linux Platform**: `uname -s` MUST be `Linux`.
- [ ] **Gate 2: Deployment Target Caveat**:
  - **This repository's code is extracted from a live, continuously-running production pipeline on a Homelab host (verified for weeks; it is the system currently feeding your AI assistant). The published code is scrubbed of personal identifiers and matches production logic.**
  - **The ONLY dimension NOT validated is remote VPS / different-distro deployment.** The code itself is proven in production. Warn the user if deploying on a cloud VPS (headless Xvfb, memory footprint for decrypting WCDB, disk I/O, security exposure).
- [ ] **Gate 3: Required Tools**: `python3`, `gdb`, `Xvfb`, `systemctl`.
- [ ] **Gate 4: Native WeChat Installed**: Check `/opt/wechat/wechat` and existence of user database folders.

### 2. Safety Rails
- NEVER bind `8765` to `0.0.0.0`. Default to `127.0.0.1` or Tailscale overlay IP.
- Enforce strict `0700` directory and `0600` file permissions on `$HOME/.local/share/wechat-vault`.
- **Auth is FAIL-CLOSED by default**: `wechat_export_api.py` REQUIRES a Bearer token (config `token` field or `--token`). With no token configured and without the explicit `--insecure-no-auth` flag, the service REFUSES to start (exit code 2). A missing token never degrades to open access.
- **`/health` is intentionally UNAUTHENTICATED**: `GET /health` / `GET /api/wechat/health` return `200` with NO token because they expose only liveness status, never chat data. Every data-bearing endpoint (especially `/api/wechat/export`) ALWAYS requires `Authorization: Bearer <TOKEN>` and returns `401` otherwise. Do not rely on the export endpoint being open in health checks or alerting probes.
