# 安全策略与漏洞披露规范 (Security Policy)

本项目致力于提供安全、纯本地、无侵入的微信数据归档与 AI 导出管道。为保护用户的个人隐私与通讯机密，本系统在架构与实现层严格遵循最小权限与纵深防御原则。

---

## 1. 核心安全承诺 (Security Guarantees)

1. **只读访问 (Strictly Read-Only)**：
   - 本项目对微信官方运行目录（`~/Documents/xwechat_files/`）**只实施只读扫描与文件硬链接读取**；
   - 严禁对微信活跃数据库执行任何写事务或注入操作，彻底杜绝数据损坏、掉线或被官方风控封号的风险。
2. **零数据外发 (Zero Outbound Exfiltration by Default)**：
   - 微信通讯录、聊天记录、图片、视频、语音转写全部在 Linux 本机内存与本地文件系统中解密和流转；
   - 除用户**显式开启并主动配置凭据**的 MinerU 文件云端解析、以及显式配置的第三方 Webhook 推送外，本系统不会向互联网发送任何聊天内容。
3. **安全凭据管理 (Ephemeral & Restricted Credential Storage)**：
   - SQLCipher 解密密钥仅常驻受限内存，落盘缓存文件严格应用 `0600` (`-rw-------`) POSIX 权限；
   - 日志系统针对密钥、Passphrase、Token 实施自动脱敏，严禁明文凭据流入系统标准输出或 `sync.log`。

---

## 2. API 接口安全与 Bearer 鉴权证据

API 服务 (`bin/wechat_export_api.py`) 采用默认**失败关闭（Fail-closed）**的设计：当未指定 Token 且未显式传入 `--insecure-no-auth` 时，服务拒绝启动。

所有导出与媒体下载端点均强制校验 HTTP 请求头中的 Bearer Token：

```python
# 摘录自 bin/wechat_export_api.py 真实源码实现:
# 1. /api/wechat/export 鉴权拦截 (bin/wechat_export_api.py:909-911)
if path == "/api/wechat/export":
    if not self.check_auth():
        self.send_json(401, {"error": "Unauthorized"})
        return

# 2. /api/wechat/media 搜索端点鉴权拦截 (bin/wechat_export_api.py:946-949)
if path == "/api/wechat/media":
    if not self.check_auth():
        self.send_json(401, {"error": "Unauthorized"})
        return

# 3. /api/wechat/media/<asset_id>/<file> 静态二进制原文件下载鉴权 (bin/wechat_export_api.py:1258-1260)
m_media_file = re.fullmatch(r"^/api/wechat/media/([0-9a-f]{32})/([^/]+)$", path)
if m_media_file:
    if not self.check_auth():
        self.send_json(401, {"error": "Unauthorized"})
        return

# 4. Bearer Token 严格匹配校验逻辑 (bin/wechat_export_api.py:856-873)
def check_auth(self) -> bool:
    if not self.server_context.get("auth_enabled", True):
        return True
    expected_token = self.server_context.get("token")
    if not expected_token:
        return False
    auth_header = self.headers.get("Authorization", "")
    parts = auth_header.strip().split()
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1] == expected_token:
        return True
    return False
```

只有存活状态探针 `/health` 免除鉴权，但其仅返回布尔型健康状态，绝不泄露任何会话数据。

---

## 3. 威胁模型与加固建议 (Threat Model & Mitigations)

| 潜在威胁场景 | 风险分析 | 官方防御与操作建议 |
| :--- | :--- | :--- |
| **同机非特权其他用户** | 共享主机环境下的非 root 用户试图通过文件系统窃取已解密的数据库。 | 1. `wechat_publish.py` 和 `wechat-sync.sh` 在每次版本发布后强制执行 `chmod -R 700 ~/.local/share/wechat-vault`；<br>2. 密钥文件及明文分片库严格设为 `chmod 600`；<br>3. 运行微信与导出服务的 Linux 用户应专人专用，禁止共用多租户主机。 |
| **误暴露至公网** | 用户将 `--host` 误设为 `0.0.0.0`，导致公网 IP 的 8765 端口直接面对扫描器。 | 1. 默认监听地址固定为 `127.0.0.1`；<br>2. 严禁直接映射至公网；若需跨机器拉取（例如 Mac 访问家服），必须经由 Tailscale / WireGuard 虚拟私网、或 SSH 端口转发 (`ssh -L 8765:127.0.0.1:8765`)；<br>3. 若确需反向代理，前面必须架设 Nginx / Caddy 并强制开启 TLS 1.3。 |
| **日志与异常泄露** | 错误堆栈打印导致 Token 或聊天片段被采集进外部日志平台。 | 1. 探针与服务对超过 11 位的身份证、手机号、银行卡实施 `***` 掩码；<br>2. 采用高熵 Token（`openssl rand -hex 32`）并定期轮换；<br>3. Token 只经环境变量或外部配置文件注入，切勿提交至代码仓库。 |

---

## 4. 安全漏洞报告 (Reporting a Vulnerability)

如果您在本项目中发现了潜在的安全漏洞或凭据泄露风险，请**切勿在公开的 GitHub Issues 中提交**。

请通过 GitHub 官方支持的 **Private Vulnerability Reporting** 私密通道报告：
1. 访问本仓库的 Security 页面：[Report a vulnerability](https://github.com/yaping-pro/wx-linux-exporter/security/advisories/new)。
2. 报告请包含明确的复现步骤、PoC 样例以及预估的影响范围。
3. 维护者会在确认收到报告后及时评估风险，并在修复方案验证通过后发布安全补丁与安全通报。
