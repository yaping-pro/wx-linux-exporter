# 媒体富化（可选层）· Media Enrichment

> **状态**：可选扩展，**默认关闭**（`config/media_policy.json` 的 `enabled` 默认 `false`）
> **契约影响**：改变 `text` 语义与 `watermark` 单调性 —— **务必先读 [`DATA_CONTRACT.md`](DATA_CONTRACT.md) 的 v1.1 章节**

---

## 1. 它解决什么问题

基线导出只给出媒体消息的**占位符**：图片 `text=""`、语音 `text=""`、文件 `text` 是原始 appmsg XML。下游 AI 助理拿不到任何内容。

本层把**能拿到的内容**解析成文本，注入既有 `text` 字段：

| 消息类型 | 富化来源 | 是否需要外部服务 |
| :--- | :--- | :--- |
| **文件附件**（pdf/docx/xlsx/pptx/html…） | MinerU 文档解析 | 是（MinerU；免 token 通道可用） |
| **语音** | **微信客户端自带的语音转文字** | 否（纯读本地库） |
| **图片** | **离线 AES+XOR 解密 + 本地 OCR** | 否（100% 离线推导与计算） |
| 视频 | 不支持（未评估） | — |
---

## 2. 架构

```
wechat-decrypt.timer → decrypted/current (只读快照)
                              │
wechat-media.timer ─→ bin/wechat_media.py ─→ 解析 ─→ state/media_ledger.json
        (5min)              │                              │
                            └─→ exports/media/<日期>/<md5>/full.md
                                                           │
wechat-export-api.service ←── 读 ledger，把 text_head 注入既有 `text`
```

- **worker 与 API 解耦**：解析是分钟级操作，绝不能内联进每次 HTTP 请求（会打爆 MinerU 配额、拖垮既有消费端的超时预算）。
- **按内容哈希寻址的物化缓存**：同一文件转发到多个会话只解析一次（`dedupe.by=md5`）。
- **幂等**：ledger 记录 `state ∈ {ready, pending, skipped, failed}`；`ready/skipped/failed` 为终态。

---

## 3. 启用步骤

```bash
# 1) 从模板生成策略（注意：默认 enabled=false）
cp config/media_policy.example.json config/media_policy.json

# 2) 填入你的会话白名单（用 username，不要用群名/备注名）
#    - 群: xxx@chatroom    - 单聊: wxid_xxx
#    名单为空 = 媒体全关
$EDITOR config/media_policy.json      # 设 "enabled": true, 填 chat_allowlist

# 3) 干跑：只打印"将要解析的清单"，不上传、不花钱
python3 bin/wechat_media.py --policy config/media_policy.json --dry-run -v

# 4) 放行
sudo systemctl enable --now wechat-media.timer
#    或手动单次：sudo systemctl start wechat-media.service
```

### 三层闸（任一不过就不花钱）

1. **会话闸**：`chat_allowlist`（用 `xxx@chatroom` / `wxid_xxx` 稳定 ID）
2. **类型闸**：`kinds.{file,image,voice,video}` + `file_ext_allow` / `skip_ext`
3. **预算闸**：`daily_file_budget`、`max_items_per_run`、`max_bytes` + md5 去重

> ⚠️ 白名单同时管「解析」和「投递」：把会话移出白名单后，其已解析内容也不再注入。

---

## 4. 文件解析：两条通道

| 维度 | agent 通道（**免 token**） | precise 通道（需 token） |
| :--- | :--- | :--- |
| 支持格式 | `pdf` / `docx` / `pptx` / `xlsx` + 图片 | 上述 + `html`（MinerU-HTML）/ `doc` / `xls` / `ppt` |
| 单文件大小 | ≤ 10 MB | ≤ 200 MB |
| 页数 | ≤ 20 页 | ≤ 200 页 |
| 超页处理 | 自动按 20 页切片后拼接 | 自动按 200 页切片后拼接 |
| 凭据 | **无** | 见 §6 |

**路由规则**：格式与体积都在 agent 覆盖内 → 走 agent；否则有 token → 走 precise；都没有 → 记 `agent_unsupported` / `oversize` / `no_credential` 并跳过（**不静默丢**）。

### 消息 → 磁盘路径的确定性关联

```
appmsg(<type>6</type>).<md5>  →  hardlink.db : file_hardlink_info_v4.md5
                              →  file_name + dir2id[dir1]（月份目录）
                              →  <xwechat_files>/<account>/msg/file/<YYYY-MM>/<file_name>
```

- `dir1` 有时不是月份而是会话哈希 → 退化为「同名文件唯一性」匹配；仍不唯一则记 `unresolved`，**不上传、不猜测**。
- 实测覆盖率：`file_hardlink_info_v4` 78 条中 68 条可解析（87%）。

### 为什么必须先 staging

`msg/file/` 是微信**实时写入的活动目录**，不在原子快照里。worker 会先复制到 `staging/media/<md5>/` 并校验大小稳定（或对齐 `hardlink.file_size`）后才上传，避免把"下载中的半截 PDF"解析成残缺正文。

---

## 5. 语音转写：零成本路径

**不需要**下载音频、不需要 SILK 解码、不需要 ASR 模型、不需要网络。

1. 在微信客户端开启：`设置 → General → Features → **Auto-convert voice messages to text**`
2. 微信服务端转写，结果写入 message 表的 **`packed_info_data`** 列
3. worker 读取该列并注入 `text`

### `packed_info_data` 结构（protobuf）

```
未转写: 08 35 10 0d 58 00                            (6 字节空壳，无 field5)
已转写: 08 35 10 0d 2a 7c 08 02 12 78 <120B 文本> 58 00
                          ^^  field5 (len) → 子消息 field2 (len) = 转写文本
```

### 状态机

| 状态 | 条件 | 后续 |
| :--- | :--- | :--- |
| `ready` | `packed_info_data` 含转写 | 注入 `text` |
| `pending` | 未转写，但消息年龄 < `readiness.max_pending_age_s` | 每轮复查，转写到达后自动转 `ready` |
| `skipped` | 超过等待窗口仍无转写 | 终态，不再复查（避免永久挂住水位） |

> **历史语音不会回溯**：自动转写只对**开启设置之后新收到**的语音生效。历史语音要么逐条在客户端点 "Convert to text"，要么保持 `voice_not_transcribed`。

### 已知事实（避免走弯路）

本机**没有语音音频实体**：`VoiceTemp/` 目录为空、全盘无 SILK/AMR 魔数、消息行 `download_status=1`（未下载）。音频只存在于腾讯 CDN。
**不要**去实现 CDN 下载协议或本地 ASR —— 上面这条路径已完全替代，且成本为零。

---

## 6. MinerU 凭据（可选）

只有需要 precise 通道时才配。三种方式，按优先级：

| 方式 | 用法 | 说明 |
| :--- | :--- | :--- |
| **systemd 加密凭据**（推荐） | 见下 | 落盘加密，仅在本服务私有 credentials 目录内解密 |
| 0600 文件 | `precise.token_file: "/path/to/token"` | 明文落盘，但简单 |
| 环境变量 | `MINERU_API_TOKEN=...` | 对 `ps -Eww` 可见，最不推荐 |

```bash
# 1) 加密落盘（host key；有 TPM 时自动用 TPM）
sudo mkdir -p /etc/wechat-vault && sudo chmod 700 /etc/wechat-vault
printf '%s' 'sk-YOUR-MINERU-TOKEN' | \
  sudo systemd-creds encrypt --name=mineru-token --with-key=host - /etc/wechat-vault/mineru-token.cred
sudo chmod 400 /etc/wechat-vault/mineru-token.cred

# 2) 在 wechat-media.service 中取消注释
#    LoadCredentialEncrypted=mineru-token:/etc/wechat-vault/mineru-token.cred
sudo systemctl daemon-reload && sudo systemctl start wechat-media.service

# 3) 生效判据：journal 出现 precise_token=available
journalctl -u wechat-media.service -n 20 | grep precise_token
```

> **本仓库不含任何令牌**。凭据只存在于你的机器上；`mineru_agent.py` 从 `$CREDENTIALS_DIRECTORY/mineru-token` → `--token-file` → 环境变量按序解析，**绝不进入 argv、绝不写日志**。

---

## 7. 运维

```bash
# 干跑（不上传）
python3 bin/wechat_media.py --policy config/media_policy.json --dry-run -v

# 单次真实运行
sudo systemctl start wechat-media.service && journalctl -u wechat-media.service -n 30

# 账本总览
python3 -c "import json;d=json.load(open('state/media_ledger.json'));\
print(d['daily']);\
from collections import Counter;print(Counter(v['state'] for v in d['assets'].values()))"

# 临时回填（默认只回溯 lookback_hours；放大窗口后跑一轮，完事改回）
#   把 lookback_hours 调到 720，然后 sudo systemctl start wechat-media.service
```

### `deny_reason` 速查

| reason | 含义 | 处置 | 消费端 `note` |
| :--- | :--- | :--- | :--- |
| `chat_not_allowlisted` | 会话不在白名单 | 加白名单 | 原 `note`（图片/语音/文件） |
| `ext_denied` / `ext_not_allowed` | 扩展名被 `skip_ext` / 白名单排除 | 调整 `file_ext_allow` | 原 `note` |
| `agent_unsupported` | 该格式 agent 通道不支持且无 token | 配 precise 凭据 | 原 `note` |
| `oversize` | 超过当前通道大小上限 | 拆分 / 配 precise | 原 `note` |
| `page_limit` | 超过页数上限且切片后仍失败 | 手动指定 `--pages` | 原 `note` |
| `missing_source` | 图片/文件从未落盘 | 微信只在点击/预览时下载，属正常现象 | `图片（未下载）` |
| `image_not_on_disk` | 图片暂未落盘（pending 态） | 消息很新，等待窗口内自动复查，不阻滞水位推进 | `图片（处理中）` |
| `unresolved:ambiguous` | 同名多档无法唯一定位 | 存在歧义，严格遵循「不猜」原则跳过 | `图片（无法定位）` |
| `missing_keys` | kvcomm 推导与内存兜底均未获取密钥 | 检查 kvcomm 统计文件或开启 `allow_sudo_memscan`，需运维介入 | `图片（不可解密）` |
| `skipped:decryptor_unavailable` | `wechat_image` 模块缺失或导入失败 | 检查环境依赖（如 pycryptodome）完整性 | `图片（不可解密）` |
| `decrypt_failed` | dat 头损坏或密钥不匹配 | 可重试有限次，重试耗尽后落 failed 终态 | `图片（解析失败）` |
| `max_pixels_exceeded` | 超过最大像素阈值（防解压炸弹） | 默认 5000 万像素，可调整 `image.max_pixels` | `图片（超大）` |
| `skipped:animated` | wxgf 多帧动图 | 按策略 `wxgf.animated="skip"` 跳过 | `图片（动图）` |
| `voice_not_transcribed` | 语音未转写且已过等待窗口 | 在客户端开启自动转写 / 手动转写 | `语音` |
| `no_credential` | 需要 precise 但未配 token | 见 §6 | `卡片/链接/文件` |
| `deduped` | 同内容已解析过 | 正常，零成本 | `图片` / 原 `note` |
---

## 8. 安全与隐私

- **媒体内容会离开本机**（仅文件解析）：MinerU 是云端服务。白名单即"我同意把这些会话的内容送上去"的显式声明。
- **图片解密与 OCR 全程本机**：密钥由本地 `kvcomm` 推导（或本地进程内存扫描），OCR 走本地引擎（RapidOCR/Tesseract），**不产生任何网络请求**；仅"文件附件解析"会调用 MinerU 云端。把会话移出白名单后，其图片不再被检索端点返回。
- **语音转写不出本机**（走微信自身通道）。
- 敏感类型可在 `file_ext_allow` 中直接排除。
- 本层**不改变鉴权模型**：`/api/wechat/export` 始终要求 Bearer Token。
- ledger 内含家服绝对路径，属本机文件（`state/` 已在 `.gitignore` 覆盖范围内，**不要提交**）。

---

## 9. 已知限制

| 限制 / 现状 | 说明 |
| :--- | :--- |
| 图片（type 3）解密实测与支持 | 18,402/18,402 全部 V2 三段式、kvcomm 离线推导（AES-128-ECB + 单字节 XOR，xor_key=0x0C）、格式分布 jpg 80.65% / wxgf 11.75% / png 7.60%、画质可及性 hd 16.07% / full 29.42% / 仅缩略图 54.51%、明文 Thumb 兜底覆盖率仅 1.5%–11% 不可行。现已支持离线解密、变体择优（_h > full > _t > _b）与本地 OCR 富化，结构化附加至 `media` 字段，同时以 `text_head`（≤2000 字符截断投影）注入既有 `text` 字段以无缝兼容存量下游 |
| 视频不支持 | 未评估 |
| 历史语音不回溯 | 见 §5 |
| 覆盖率受限于"微信是否已下载" | 文件是**点击才下载**；headless 客户端不会自动落盘所有附件，因此 `missing_source` 属正常现象 |
| `hardlink.db` 表结构随微信版本变化 | 升级微信后需回归验证 `file_hardlink_info_v4` |
| precise 通道依赖外部服务可用性 | 限流/额度耗尽会落 `throttled` / `mineru:8`，可重试 |
