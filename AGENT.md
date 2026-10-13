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
  - **本项目仅在家庭/局域网常驻闲置 Linux 主机（Homelab）经过完整实机验证**。
  - **未在公网 VPS 进行过生产级验证**。若检测到目标环境为纯 VPS 或云服务器，Agent 必须显式向用户发出警示，告知其关注：
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
以普通用户权限运行（或通过系统 systemd 并注入 User 用户名）：
```bash
USER_NAME=$(whoami)
# 安装服务模板，将 %I 替换为当前用户
sudo cp deploy/systemd/wechat-*.service /etc/systemd/system/
sudo cp deploy/systemd/wechat-*.timer /etc/systemd/system/
sudo sed -i "s/%I/$USER_NAME/g" /etc/systemd/system/wechat-*.service

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
- 🔴 **禁止在微信主进程执行数据库事务写操作**，整个流水线基于副本解密与 WAL 内存合并，严禁修改原库。

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
  - **This pipeline has been thoroughly verified ONLY on a standby Linux Homelab machine.**
  - **NOT verified on remote VPS.** Warn the user if deployed on a cloud VPS (headless Xvfb, memory footprint for decrypting WCDB, disk I/O, security exposure).
- [ ] **Gate 3: Required Tools**: `python3`, `gdb`, `Xvfb`, `systemctl`.
- [ ] **Gate 4: Native WeChat Installed**: Check `/opt/wechat/wechat` and existence of user database folders.

### 2. Safety Rails
- NEVER bind `8765` to `0.0.0.0`. Default to `127.0.0.1` or Tailscale overlay IP.
- Enforce strict `0700` directory and `0600` file permissions on `$HOME/.local/share/wechat-vault`.
- Decryption is completely read-only on cloned database files. Never touch live WeChat process files in write mode.
