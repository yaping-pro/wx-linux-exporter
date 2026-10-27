# 5 分钟极速上手指南 (Quickstart Guide)

本文档面向首次接触本项目的工程师与系统管理员，提供在全新 Linux 环境下从零解密、导出并在 5 分钟内验证微信数据的确定性实操指引。

---

## 1. 前置条件检查

在开始前，请确保满足以下条件：
- **操作系统**：Linux x86_64 / aarch64 (Ubuntu 22.04+, Debian 12+, Rocky/RHEL 9+)；
- **Python 环境**：Python 3.10+，安装标准依赖：
  ```bash
  pip install pycryptodome
  ```
- **微信状态**：已登录 Linux 原生微信 4.x（或经由 `Xvfb :99` 启动的无头微信实例）。
- **核对数据目录**：微信 4.x 本地数据与消息分片数据库位于 `~/Documents/xwechat_files/<wxid>/`：
  ```bash
  ls -d ~/Documents/xwechat_files/wxid_*/db_storage
  ```
  *预期输出*：
  ```text
  /home/username/Documents/xwechat_files/wxid_84wifrc2ztsx22_5d60/db_storage
  ```

---

## 2. 数据库解密与原子发布

本项目采用「只读提取密钥 → 离线解密分片副本 → 合并 WAL 增量帧 → 原子发布至 `decrypted/current`」的无损安全架构。

### 步骤 2.1 运行同步解密流水线
运行项目提供的 `bin/wechat-sync.sh`（内部调度 `extract_keys.py` 与 `wechat_publish.py`）：
```bash
bash bin/wechat-sync.sh
```

*预期输出*：
```text
[INFO] 检测到微信数据库源目录: /home/username/Documents/xwechat_files/wxid_xxx/db_storage
[INFO] 开始原子发布微信数据库版本 ...
[INFO] Decrypted version created: 20261027T081530Z-12345
[INFO] WAL merge complete: 14 databases patched
[INFO] Symlink updated: ~/.local/share/wechat-vault/decrypted/current -> versions/20261027T081530Z-12345
[INFO] 微信数据库同步流水线执行完成。
```

验证解密目录：
```bash
ls -la ~/.local/share/wechat-vault/decrypted/current/
```
可以看到解密后的明文数据库：`contact/contact.db`、`session/session.db`、`message/message_0.db` 等。

---

## 3. 初始化 Vault 目录与媒体策略配置

```bash
# 1. 创建基础配置目录与状态目录
mkdir -p ~/.local/share/wechat-vault/{config,state,exports}

# 2. 写入初始 media_policy.json（默认先保持 enabled=false，只导纯文本）
cat << 'EOF' > ~/.local/share/wechat-vault/config/media_policy.json
{
  "enabled": false,
  "chat_allowlist": [],
  "kinds": {
    "file": true,
    "image": false,
    "voice": true,
    "video": false
  },
  "image": {
    "enabled": false,
    "ocr": {
      "enabled": false,
      "providers": ["rapidocr"]
    }
  },
  "video": {
    "enabled": false
  }
}
EOF
```

---

## 4. 启动导出 API 服务 (`wechat_export_api.py`)

服务原生强制 **Fail-closed 鉴权保护**（没有 Token 拒绝启动）。

### 步骤 4.1 生成随机高熵 Token
```bash
export WECHAT_TOKEN=$(openssl rand -hex 32)
echo "Generated Token: ${WECHAT_TOKEN}"
```

### 步骤 4.2 启动只读 API 进程
```bash
python3 bin/wechat_export_api.py \
  --host 127.0.0.1 \
  --port 8765 \
  --vault-dir ~/.local/share/wechat-vault/decrypted/current \
  --token "${WECHAT_TOKEN}"
```

*预期控制台输出*：
```text
[INFO] Starting WeChat Export API on 127.0.0.1:8765
[INFO] Vault directory: /home/username/.local/share/wechat-vault/decrypted/current
[INFO] Token auth enabled: True
```

---

## 5. 验证端点连通性 (5 分钟验证)

在另一个终端使用 `curl` 验证导出接口：

### 5.1 纯文本基线验证 (免富化开销)
```bash
curl -s -H "Authorization: Bearer ${WECHAT_TOKEN}" \
  "http://127.0.0.1:8765/api/wechat/export?media=none&ocr=none" | jq '{watermark, message_count: .manifest.message_count, chats_count: (.chats | length)}'
```

*预期输出*：
```json
{
  "watermark": "1759123456",
  "message_count": 86,
  "chats_count": 5
}
```

### 5.2 验证存活探针 (无需鉴权)
```bash
curl -s "http://127.0.0.1:8765/health" | jq .
```
*预期输出*：
```json
{
  "status": "ok",
  "auth": "bearer",
  "vault_present": true
}
```

---

## 6. 可选：启用图片与视频富化 (On-demand Enrichment)

当业务端或个人 AI 助理需要查看原图、视频以及 OCR 文本时，可通过配置与查询参数按需开启。

### 6.1 开启图片 (`kinds.image=true`)
编辑 `~/.local/share/wechat-vault/config/media_policy.json`：
```json
{
  "enabled": true,
  "chat_allowlist": ["<你的群聊ID>@chatroom"],
  "kinds": {
    "file": false,
    "image": true,
    "voice": true,
    "video": false
  },
  "image": {
    "enabled": true,
    "ocr": {
      "enabled": true,
      "providers": ["rapidocr"]
    }
  }
}
```
运行后台媒体处理器：
```bash
python3 bin/wechat_media.py \
  --policy ~/.local/share/wechat-vault/config/media_policy.json \
  --vault ~/.local/share/wechat-vault \
  --max-items 20 -v
```

### 6.2 开启视频 (`kinds.video=true`)
在 `media_policy.json` 中配置 `kinds.video: true`，`wechat_media.py` 会自动扫描硬链接并提取视频元数据与封面图（`poster`）。
> ⚠️ **视频实机说明**：由于微信 4.x 视频以明文 MP4 或本地缓存落地，部分受保护频道的加密视频目前未在实机全量验证；worker 采用安全降级策略，遇无法解密视频将标记为 `note: "视频（不可解密）"`，不会阻断导出流水线。

---

## 7. 常见故障排查表 (Troubleshooting FAQ)

| 症状 / 错误码 | 根本原因 | 解决办法 |
| :--- | :--- | :--- |
| **HTTP 401 Unauthorized** | 请求未带 `Authorization: Bearer <TOKEN>` 头，或 Token 字符串不匹配。 | 确认启动命令中的 `--token` 与 `curl -H "Authorization: Bearer ..."` 严格一致。严禁在非测试环境使用 `--insecure-no-auth`。 |
| **`note: "图片（未下载）"` (`missing_source`)** | 微信客户端仅接收到缩略图，用户或后台未在微信内触发原图下载。 | 正常现象。在微信聊天窗口中点击该图片触发原图下载，下一次运行 `wechat_media.py` 即可自动补齐富化。 |
| **`encrypted_video_unverified` / 视频不可解密** | 视频属于特定加密流或外部小程序视频载体，本地尚未解密密钥。 | worker 自动兜底降级，元数据保留 `url` 与会话定位，建议消费端通过消息上下文判断是否需手动介入。 |
| **RapidOCR / Tesseract 装不上** | 缺乏 C++ 编译依赖、onnxruntime 兼容库或系统内存过小 (≤1GB)。 | 1. 优先使用 Python Wheel：`pip install rapidocr-onnxruntime`；<br>2. 若下游是自带多模态大模型的 Muse，**完全不需要安装 OCR**，设置 `ocr: {"enabled": false}` 即可直接把图片 URL 喂给视觉模型。 |
| **`FATAL: No auth token configured`** | `wechat_export_api.py` 启动时既没有提供 `--token`，也没有在配置文件中填 `token`。 | 必须显式传入 `--token $(openssl rand -hex 32)`。本项目实施 Fail-closed 安全策略，拒绝在无鉴权状态下意外监听公网。 |
