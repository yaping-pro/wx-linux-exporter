# WeChat Linux Exporter (`wx-linux-exporter`)

> **Linux 原生微信数据库解密导出管道 + 个人 AI 助理 (muse.ai 风格) 归档服务**  
> Native Linux PC-WeChat decryption, atomic versioned export, and personal assistant bridge pipeline.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/yaping-pro/wx-linux-exporter?color=brightgreen)](https://github.com/yaping-pro/wx-linux-exporter/releases/latest)
[![CI & Release](https://github.com/yaping-pro/wx-linux-exporter/actions/workflows/ci.yml/badge.svg)](https://github.com/yaping-pro/wx-linux-exporter/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)

[中文](#中文) | [English](#english)

---


## 中文

### 目录
- [⚡ 30 秒极速上手 (Quickstart)](#-30-秒极速上手-quickstart)
- [⚠️ 运行环境真实性验证声明 (Homelab vs VPS)](#️-运行环境真实性验证声明-homelab-vs-vps)
- [🤖 交给你的 AI Agent (Autonomous Agent Runbook)](#-交给你的-ai-agent-autonomous-agent-runbook)
- [💡 项目定位与核心特性](#-项目定位与核心特性)
- [🛠️ 从源码安装与构建](#️-从源码安装与构建)
- [⚙️ 服务编排与 Systemd 配置](#️-服务编排与-systemd-配置)
- [🤖 接入个人 AI 助理 (Muse 早报 / Webhook)](#-接入个人-ai-助理-muse-早报--webhook)
- [📊 数据契约与导出 Schema](#-数据契约与导出-schema)
- [❓ 常见问题与排错指南 (FAQ)](#-常见问题与排错指南-faq)
- [🤝 兄弟项目 (Sibling Projects)](#-兄弟项目-sibling-projects)
- [💬 交流群 / Community](#-交流群--community)
- [🔒 安全政策与免责声明](#-安全政策与免责声明)
- [📄 开源许可证](#-开源许可证)

---

### ⚡ 30 秒极速上手 (Quickstart)

推荐直接从 [GitHub Releases](https://github.com/yaping-pro/wx-linux-exporter/releases/latest) 下载打包好的便携归档：

```bash
# 1. 下载并解压最新发布包
TAG=$(curl -s https://api.github.com/repos/yaping-pro/wx-linux-exporter/releases/latest | grep -Po '"tag_name": "\K.*?(?=")')
wget "https://github.com/yaping-pro/wx-linux-exporter/releases/download/${TAG}/wx-linux-exporter-${TAG}.tar.gz"
tar -xzf "wx-linux-exporter-${TAG}.tar.gz" -C ~/.local/share/wechat-vault/

# 2. 生成安全 Token 并初始化配置
cd ~/.local/share/wechat-vault
TOKEN=$(openssl rand -hex 32)
sed -i "s/REPLACE_WITH_YOUR_AUTH_TOKEN/${TOKEN}/g" config/export_service.json

# 3. 运行自检
bash scripts/selfcheck.sh

# 4. 启动只读 API 服务 (默认绑定 127.0.0.1:8765)
python3 bin/wechat_export_api.py --config config/export_service.json
```

---

### ⚠️ 运行环境真实性验证声明 (Homelab vs VPS)

> **【极其重要：诚实性与边界声明】**
> 本仓库代码**提取自一套已在家庭服务器（Homelab）上连续、长效运行的真实生产流水线**（decrypt → publish → serve 全链路已稳定运行数周，正是当前向你个人 AI 助理持续供数的那套系统）。发布版本已对路径、IP、鉴权 Token 等个人标识做了脱敏（scrub）处理，逻辑与线上一致。
> **唯一未经充分验证的维度是「部署到远程 VPS / 不同 Linux 发行版」**：代码逻辑本身已在生产环境被持续运行验证，绝非未经测试的 prototype。

如果你计划在远程 VPS 上部署本服务，请务必特别注意以下潜在风险与技术挑战：
1. **Systemd 与无头显示环境**：VPS 通常无物理显示输出，必须依赖 `Xvfb :99` 提供虚拟显示缓冲；部分轻量容器型 VPS（如 OpenVZ / LXC）可能缺少完整的 systemd 用户会话支持。
2. **磁盘 I/O 与解密负载**：解密全量 WCDB 数据库及二进制合并 WAL 日志属于高 I/O 密集型操作，低配置 VPS 可能会遭遇 I/O 挤压。
3. **网络安全与端口暴露**：服务默认**仅监听 `127.0.0.1`**。严禁将 `8765` 端口直接暴露在公网！在 VPS 上访问建议严格通过 Tailscale、WireGuard 虚拟私网或 SSH 本地端口转发 (`ssh -L 8765:127.0.0.1:8765`)。
4. **内存与 Swap 空间**：解密大容量消息数据库时需要足够的空闲内存（建议物理内存 ≥ 4GB），否则可能触发 OOM 导致微信或解密脚本崩溃。
5. **权限与 UID**：GDB 附加微信进程提取密钥需要 `ptrace` 权限（`CAP_SYS_PTRACE` 或调整 `kernel.yama.ptrace_scope`）。

---

### 🤖 交给你的 AI Agent (Autonomous Agent Runbook)

如果你使用的是 AI 辅助编码或自动化运维智能体（如 Pi / omp、Claude Code、Codex、Cursor 等），**你可以直接将部署全权交给 Agent 执行**。

详细的确定性运行手册、硬门禁与回滚机制详见：  
👉 **[`AGENT.md`](AGENT.md)**

```bash
# 直接对你的 AI Agent 下达指令
"请阅读 AGENT.md，在当前 Linux 主机上全自动部署 wx-linux-exporter 并完成健康检查。"
```

---

### 💡 项目定位与核心特性

- **纯标准库与无侵入只读**：不修改微信原库，只读拷贝副本解密，彻底杜绝数据损坏风险。
- **WAL 帧无损合并**：原生实现 `wechat_wal.py` 帧解析与内存合并，离线导出即可捕获最新实时消息。
- **原子版本发布**：采用 `<UTC_TS>-<PID>` 版本树，经 `os.replace` 原子切换 `current` 软链接，支持毫秒级一键回滚 (`vault-rollback.sh`)。
- **双端同构数据契约**：与手机端 `wx-pad-exporter` 共享相同的 `export.schema.json` 规范与 SHA-256 数据防篡改签名。

---

### 🛠️ 从源码安装与构建

```bash
git clone https://github.com/yaping-pro/wx-linux-exporter.git
cd wx-linux-exporter

# 安装 Python 依赖
pip3 install pycryptodome zstandard

# 运行语法与规范检查
bash scripts/selfcheck.sh
```

---

### ⚙️ 服务编排与 Systemd 配置

本项目提供了一套开箱即用的 systemd 服务单元（位于 `deploy/systemd/`）：
- `wechat-xvfb.service`：维持 `:99` 虚拟帧缓冲。
- `wechat-runner.service`：保持原生 Linux 微信常驻。
- `wechat-decrypt.timer`：每 15 分钟触发一次 `wechat-sync.sh` 执行增量解密与原子发布。
- `wechat-export-api.service`：以不可变方式只读提供 HTTP 查询快照。

```bash
USER=$(whoami)
sudo cp deploy/systemd/wechat-*.service /etc/systemd/system/
sudo cp deploy/systemd/wechat-*.timer /etc/systemd/system/
sudo sed -i "s/%I/$USER/g" /etc/systemd/system/wechat-*.service

sudo systemctl daemon-reload
sudo systemctl enable --now wechat-xvfb wechat-runner wechat-decrypt.timer wechat-export-api
```

---

### 🤖 接入个人 AI 助理 (Muse 早报 / Webhook)

通过仓库自带的客户端脚本，可轻松将解密流接入下游消费系统：
- **Muse 早报模式**：`python3 clients/fetch_export.py`（基于游标单调推进，提取最新未读聊天生成早报）。
- **Webhook 模式**：`python3 clients/assistant_webhook.py`（将增量快照推送到飞书、Dify 或自定义 Webhook 端点）。

详见 [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md)。

---

### 📊 数据契约与导出 Schema

本项目与兄弟项目 `wx-pad-exporter` 100% 共享相同的数据契约规范：
- JSON Schema 校验文件：[`schema/export.schema.json`](schema/export.schema.json)
- 数据格式标准定义：[`docs/DATA_CONTRACT.md`](docs/DATA_CONTRACT.md)

---

### 🤝 兄弟项目 (Sibling Projects)

本项目是微信个人知识库与 AI 助理导出家族的三部曲之一：
1. **[`yaping-pro/wx-tablet-mode`](https://github.com/yaping-pro/wx-tablet-mode)** (GPL-3.0) — 安卓微信平板模式登录资格赋予与外观伪装 Xposed 模块。
2. **[`yaping-pro/wx-pad-exporter`](https://github.com/yaping-pro/wx-pad-exporter)** (MIT) — 手机/平板端微信实时聊天流只读导出守护进程 + Webhook 桥接（数据出口格式与本项目 100% 保持一致）。
3. **`yaping-pro/wx-linux-exporter`** (MIT, 本仓库) — **Linux / Homelab 原生微信定时解密、WAL 二进制合并、原子版本发布与只读快照导出服务（基准参考实现）**。

### 💬 交流群 / Community

欢迎加入微信个人助理与数据管道交流群（探讨 Homelab 微信常驻、AI 助理搭建、数据管道等）：

<p align="center">
  <img src="assets/wechat-group-qr.png" alt="微信交流群" width="220" /><br>
  <b>微信个人助理交流</b>
</p>

> ⚠️ **群二维码时效提示**：微信群二维码有效期为 7 天（当前二维码**有效至 2026-10-20**）。若二维码已过期，请查阅仓库最新提交或开 Issue 提醒维护者更新。  
<!-- Maintainer note: rotate assets/wechat-group-qr.png when the group QR expires. -->

---

### 🔒 安全政策与免责声明

- 本项目遵循 [SECURITY.md](SECURITY.md) 安全披露规范。
- 完整使用边界与合规说明详见 [DISCLAIMER.md](DISCLAIMER.md)。

---

### 📄 开源许可证

本项目基于 [MIT License](LICENSE) 开源发布。

---

## English

### 🤝 Sibling Projects
- **[`yaping-pro/wx-tablet-mode`](https://github.com/yaping-pro/wx-tablet-mode)** (GPL-3.0) — WeChat Android Tablet-mode login & disguise hook.
- **[`yaping-pro/wx-pad-exporter`](https://github.com/yaping-pro/wx-pad-exporter)** (MIT) — Real-time read-only chatstream exporter daemon for phone/tablet WeChat.
- **`yaping-pro/wx-linux-exporter`** (MIT, this repo) — **Homelab Linux PC-WeChat decrypt -> publish -> serve reference pipeline.**

### ⚠️ Deployment Target Disclaimer (Homelab vs VPS)
> **Notice**: This repository's code is **extracted directly from a live, continuously-running production WeChat pipeline on a Homelab host (Ubuntu 22.04/24.04)** — the decrypt → publish → serve chain has run uninterrupted for weeks and is the system that currently feeds your personal AI assistant. The published code is that pipeline, scrubbed of personal identifiers (paths, IPs, auth tokens); its logic matches production.
> **The ONLY dimension that has NOT been validated is deployment on a remote VPS / different distro.** The code itself is proven in production, not an untested prototype.
> If you deploy on a cloud VPS:
> 1. Ensure `Xvfb` is operational for headless UI rendering.
> 2. Be aware of disk I/O and memory limits during WCDB decrypts.
> 3. **NEVER expose port 8765 to the public Internet.** Bind to `127.0.0.1` or access via Tailscale / SSH tunnels.
> 4. Ensure sufficient swap/RAM (>= 4GB recommended) to prevent OOM kills.
