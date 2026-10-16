# SETUP GUIDE (`SETUP_GUIDE.md`)

[中文](#中文) | [English](#english)

---

## 中文

### 1. 架构总览
`wx-linux-exporter` 在 Linux 环境下实现了一套完整的解密与导出管道：
1. **运行环境**：利用 `Xvfb :99` 在无实体显示器的主机上无头运行原生 Linux 微信客户端。
2. **密钥提取**：通过 `wcdb-key-tool` 基于 GDB / ELF 静态分析从运行中的微信进程安全提取 SQLCipher 密钥。
3. **定时同步与原子发布**：`wechat-sync.sh` 定时触发，通过 `wechat_publish.py` 将数据库副本复制并解密到 `<UTC_TS>-<PID>` 目录，合并 WAL 帧后原子切换 `current` 软链接。
4. **只读 API 服务**：`wechat_export_api.py` 常驻运行，绑定回环或私网 IP（如 Tailscale），提供严格 Bearer Token 鉴权的只读快照导出。**鉴权默认失败关闭（fail-closed）**：未配置 token 时服务拒绝启动（除非显式传入 `--insecure-no-auth`，仅限可信回环/dev）；`/health` 存活探针免鉴权，其余端点始终要求 Bearer Token。详见 `docs/DATA_CONTRACT.md` 与 `AGENT.md`。
### 2. 部署与配置步骤
详见根目录 [AGENT.md](../AGENT.md) 或 [README.md](../README.md)。

#### 可选：启用媒体富化（默认关闭）

```bash
cp config/media_policy.example.json config/media_policy.json   # enabled 默认 false
$EDITOR config/media_policy.json        # 填 chat_allowlist（xxx@chatroom / wxid_xxx），设 enabled=true
python3 bin/wechat_media.py --policy config/media_policy.json --dry-run -v   # 干跑，不上传
sudo systemctl enable --now wechat-media.timer
```

- 文件解析：默认走 MinerU **免 token** 通道（≤10MB/≤20 页）；需要 html/doc/xls/ppt 或更大文件时再配 precise 凭据（见 [`MEDIA_ENRICHMENT.md`](MEDIA_ENRICHMENT.md) §6）。
- 语音转写：需在**微信客户端**开启 `设置 → General → Features → Auto-convert voice messages to text`；worker 侧零配置、零成本。
- 启用前请先读 [`DATA_CONTRACT.md`](DATA_CONTRACT.md) 顶部的 **v1.1 语义变更**（`text` 不可作去重键、`watermark` 可能回退）。

---

## English

### 1. Architectural Overview
`wx-linux-exporter` provides an end-to-end decryption and export pipeline for headless Linux hosts:
1. **Headless Execution**: Native WeChat runs headlessly via `Xvfb :99`.
2. **Key Extraction**: Encryption keys are extracted via `wcdb-key-tool` and cached securely.
3. **Atomic Decryption & WAL Merge**: `wechat_publish.py` clones active databases, decrypts pages, merges WAL frames into versioned directories, and atomically swings the `current` symlink.
4. **Read-Only API Server**: `wechat_export_api.py` serves immutable JSON snapshots strictly authenticated via Bearer tokens. **Auth is fail-closed by default**: with no token configured the service refuses to start (unless explicitly launched with `--insecure-no-auth`, trusted loopback/dev only); the `/health` liveness probe is unauthenticated while every other endpoint always requires a Bearer token. See `docs/DATA_CONTRACT.md` and `AGENT.md`.
