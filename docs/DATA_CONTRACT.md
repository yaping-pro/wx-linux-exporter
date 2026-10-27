# 微信导出数据契约规范 (Data Egress Contract)

> **版本**：`v1.3.1` (2026-10-27)

> ## ⚠️ v1.3.1 语义扩充（分页 + 流式输出 + OpenAPI 3.1 契约规范）
>
> 自 v1.3.1 起，`/api/wechat/export` 新增游标分页与 ndjson 流式输出能力，默认行为完全不变（纯增量）：
>
> | 参数 | 取值 | 默认 | 语义与消费端指引 |
> | :--- | :--- | :--- | :--- |
> | `limit` | 非负整数（`0`=不限） | `0` | 本次响应最多返回多少条消息（跨所有会话，按 `(create_time, chat_id, local_id)` 升序推进） |
> | `cursor` | 不透明字符串（base64url） | 空 | 游标续传，从该位置之后继续；格式为 `base64url({"ts":int,"chat_id":str,"local_id":int})` |
> | `format` | `json` \| `ndjson` | `json` | 传输格式：`json` 为完整 JSON 响应；`ndjson` 为分块流式输出（`application/x-ndjson`） |
>
> **分页与水位推进原则**：
> - 响应根新增 `"next_cursor": "<str>|null"`（还有剩余则给出，否则 `null`）。`limit=0` 时 `next_cursor` 恒为 `null`。
> - `payload_stats` 新增 `"truncated": true|false`（当触发 limit 且仍有后续消息时为 `true`）。
> - **分页不提前推进 `watermark`**：仅当"全部已结算"（`next_cursor` 为 `null`）时 `watermark` 正常推进；有 `next_cursor` 时，响应中的 `watermark` 保持上一轮值（`since`），消费端不可在此阶段以当前消息推进下一次 since。
>
> **流式输出格式（`format=ndjson`）**：
> - 响应头 `Content-Type: application/x-ndjson`，采用分块传输编码（`Transfer-Encoding: chunked`）。
> - 行序列规范：
>   1. 首行 `{"type":"manifest","exported_at":...,"range":...,"watermark":...,"manifest":...}`（不含 messages 数组）；
>   2. 会话元信息行 `{"type":"chat","chat_id":...,"name":...,"kind":...}`；
>   3. 消息明细行 `{"type":"message","chat_id":...,"ts":...,"sender":...,"kind":...,"text":...,"note":...}`（字段与 JSON 模式下 messages 内对象逐字段一致）；
>   4. 末行 `{"type":"end","next_cursor":...,"payload_stats":{...}}`。
> - 边计算边写入，与 `limit`、`cursor`、`media`、`ocr` 完全正交兼容。
>
> ## ⚠️ v1.3.0 历史语义（三个正交过滤开关 + 视频注入 + 体积统计）
>
> 自 v1.3.0 起，新增三个正交查询开关、视频富化注入与响应根体积统计 `payload_stats`，默认值严格向后兼容 v1.2：
> | 参数 | 取值 | 默认 | 语义与消费端指引 |
> | :--- | :--- | :--- | :--- |
> | `media` | `none` \| `image` \| `video` \| `all` | `all` | 只对选中 kind 注入 `media` 对象；未选中的媒体消息**保留 `note`**（「图片」「视频」），消费端可事后用 `/api/wechat/media?q=` 按需取 |
> | `media_chats` | 逗号分隔 chat_id | 空（全部允许会话） | 仅这些会话注入 `media`，其余会话的媒体消息退化为 `note` |
> | `ocr` | `none` \| `head` \| `full` | `head` | `none`：不注入 `ocr` 对象且 `text` 不含 OCR；`head`：仅 `text` 带 ≤2000 字符投影；`full`：`text` 投影 + `media.ocr.text` 全文 |
>
> **视频富化注入（media 对象）**：
> ```json
> {
>   "asset_id": "<32hex>", "kind": "video", "url": "/api/wechat/media/<id>/video.mp4",
>   "poster": "/api/wechat/media/<id>/thumb.jpg", "duration_s": 12, "format": "mp4",
>   "bytes": 123456, "width": 1080, "height": 1920,
>   "anchor": {"chat_id": "...", "chat_name": "...", "local_id": 123, "ts": 1759123456, "sender": "..."},
>   "source": "hardlink"
> }
> ```
> **体积统计（根 payload_stats 字段）**：
> ```json
> "payload_stats": {"messages": 123, "media_included": 45, "ocr_chars": 6789}
> ```
> `ocr=none` 时 `ocr_chars` 必须为 0。
>
> **向后兼容性声明**：
> 当缺省请求参数（或传入 `media=all&ocr=head`）时，既有导出结构与内容完全保持向后兼容。
>
> ## ⚠️ v1.2 历史语义扩充（新增结构化「媒体对象」与检索端点）
>
> 自 v1.2.0 起，本契约在保留 v1.1 基础的同时，为富化就绪的图片消息引入可选的结构化 `media` 字段及配套的媒体检索/获取端点：
>
> | # | 变更 | 核心语义与消费端指引 |
> | :--- | :--- | :--- |
> | 1 | **图片消息新增可选 `media` 对象** | 未就绪或未启用时字段完全不出现（不填 `null`）。内部包含 `asset_id`、`quality`、`format`、`width`、`height`、`bytes`、`path`、`url`、`anchor`、`source`、`ocr` 等结构化明细。 |
> | 2 | **`text` 作为 OCR 文本的截断投影** | 当图片媒体富化就绪时，`text` 填入 `media.ocr.text` 的截断投影（≤2000 字符，超出加 `…`），以无缝兼容仅读取 `text` 的既有消费端；未就绪或无文字时保持空字符串。单一事实来源仍为 ledger 与 `media.ocr.text`。 |
> | 3 | **`anchor` 定位与 `chat_name`** | 每条消息自带独立的 `anchor`（含 `chat_id`, `chat_name`, `local_id`, `ts`, `sender`），作为回到原会话的唯一凭据；`chat_name` 优先取解密备注名/群名，未命中则回退 `chat_id`。 |
> | 4 | **单调游标与终态保证** | watermark 保持单调推进；仅未就绪且在等待窗口内的临时状态挂起水位，终态（ready/skipped/failed）绝不阻滞水位。 |
> | 5 | **媒体 `note` 状态细化** | 图片/视频未就绪或跳过时细化 `note`：`图片（处理中）`、`视频（处理中）`、`视频（未下载）`、`视频（超大）`、`视频（解析失败）`；就绪时分别为 `图片`、`视频`。 |
>
> **消费端读取最佳实践**：
> - 文本检索与智能体阅读：优先读取 `media.ocr.text`。
> - 原图与视频展示：使用 `media.url` 发起携带 Bearer Token 的 GET 请求获取媒体二进制（视频支持 HTTP Range 206 断点播放）。
> - 对话定位与上下文回溯：依据 `media.anchor` 确定会话位置，或直接在 `/api/wechat/media` 检索时附带 `include=context` 参数。
> - **即用检索命令**（按关键词检索图片并内联紧邻上下文）：
>   ```bash
>   curl -s -H "Authorization: Bearer <TOKEN>" "http://<HOST>:8765/api/wechat/media?q=发票&include=context&limit=10" | jq .
>   ```
> ## ⚠️ v1.1 历史语义变更（语音与文件富化）
>
> 自 v1.1.0 起支持文件与语音富化：
> | # | 变更 | 对消费端的要求 |
> | :--- | :--- | :--- |
> | 1 | **`text` 字段动态富化** | 语音填入转写文本；文件在原有 appmsg XML 之后**追加** Markdown 正文。严禁把 `text` 作为缓存或去重键。 |
> | 2 | **`watermark` 回退机制** | 包含未就绪媒体时可能小幅回退以便复拉；消费端不可假定其物理单调递增。 |
> | 3 | **白名单会话作用域** | 富化只在 `chat_allowlist` 内生效。 |
>
> **推荐的稳定消息标识**（不含 `text`）：
> `MsgUID = SHA256(chat_id_or_name + "|" + ts + "|" + sender)`
> **适用范围**：`wx-pad-exporter` (安卓 Pad 守护进程) 及 Linux 家服 (`wechat-export-api`)  
> **Schema 定义**：[`schema/export.schema.json`](../schema/export.schema.json)

---

## 1. 概述与核心哲学 (Overview & Core Philosophy)

微信导出数据契约定义了从采集端（无论是运行于 Android 的 `wx-pad-exporter` 守护进程，还是部署于 Linux 家服的 PC 端微信数据库解密导出服务）向各类下游智能体（AI Assistant、早报生成器、知识库检索系统）传输聊天数据的标准只读协议。

### 核心设计原则
1. **快照不可变与零读撕裂 (Immutable Snapshot & Zero Read Tear)**：
   - 导出的每一批数据均为确定性、时间切片绑定的只读快照。
   - 底层存储采用只读不可变模式（家服端 `mode=ro&immutable=1`；安卓端动态 WAL 提交帧实时入库），彻底杜绝并发锁冲突与未完成事务导致的 `database is malformed` 脏读。
2. **规范化完整性与防篡改 (Canonical Hash Verification)**：
   - 响应体必须附带 `manifest.sha256`。该摘要由 `chats` 数组经过严格的规范化 JSON（Canonical JSON: 字典序键名、紧凑分隔符、UTF-8 字符保持）序列化后计算得出，下游消费端可实现亚毫秒级的数据完整性校验与重复抓取排重。
3. **单调游标与增量语义 (Monotonic Cursor & Watermark)**：
   - 响应包含唯一的 `watermark` 游标（RFC3339 时间戳），代表本批次所含消息的最大物理时间点。消费端持久化此游标后即可在下一次请求中作为 `?since=<watermark>` 传入，实现无缝断点续传。
4. **超集兼容与平滑降级 (Superset Compatibility & Graceful Fallback)**：
   - 移动端 `wx-pad-exporter` 输出是家服 PC 导出契约的**超集**。基础消费者仅需读取 `name`, `kind`, `messages` 三个核心字段；高级消费者则可提取 `chat_id`, `display_name`, `is_group` 开展更细粒度的实体关联。

---

## 2. 接口端点契约 (Standard Interface Contract)

### 2.1 基础网络与安全边界

| 参数项 | 手机端 (`wx-pad-exporter`) | 家服端 (`wechat-export-api`) |
| :--- | :--- | :--- |
| **默认监听地址** | `127.0.0.1:8765` (本地回环) | `<TAILNET_IP>:8765` (Tailscale 私网裸 IP) |
| **推荐访问通道** | `adb forward tcp:8765 tcp:8765`<br/>或 Tailscale 私网隧道 | Tailscale 点对点加密通道 (`100.64.0.0/10`) |
| **鉴权机制** | **强制必须** `Authorization: Bearer <TOKEN>`<br/>*(仅 `/health` 存活探针免鉴权；默认失败关闭，无 token 拒绝启动)* | **强制必须** `Authorization: Bearer <TOKEN>`<br/>*(仅 `/health` 存活探针免鉴权；默认失败关闭，无 token 拒绝启动)* |
| **时区规范** | `Asia/Shanghai (+08:00)` | `Asia/Shanghai (+08:00)` |
| **公网策略** | **严禁公网暴露**，零端口映射 | **严禁公网暴露**，零端口映射 |

### 2.2 端点规范

#### (1) `GET /health` (或 `GET /api/wechat/health`)
- **功能**：服务存活、运行指标与采集连接健康探针。
- **鉴权（重要）**：**此端点无需鉴权（UNAUTHENTICATED）**。它是显式设计的存活探针，仅返回服务状态，**不泄露任何聊天数据**。它刻意绕过 Bearer Token 校验——负载均衡器、K8s 探针与本地自检可在不持有密钥的情况下探测服务存活。
  > ⚠️ 注意：只有 `/health` 与 `/api/wechat/health` 免鉴权。所有承载聊天数据的端点（特别是 `/api/wechat/export`）**始终要求** `Authorization: Bearer <TOKEN>`，无 token 一律返回 `401`。`wx-linux-exporter` 的鉴权**默认失败关闭（fail-closed）**：若配置中未设置 token 且未显式传入 `--insecure-no-auth`，服务将拒绝启动。
- **状态码**：`200 OK`
- **家服端响应体示例**（`wechat-export-api` 仅返回最小状态对象）：
```json
{ "status": "ok" }
```
#### (2) `GET /api/wechat/export`
- **功能**：增量/全量拉取结构化消息快照。
- **请求参数**：
  | 参数名 | 类型 | 必填 | 默认值 | 说明与示例 |
  | :--- | :--- | :--- | :--- | :--- |
  | `since` | string/int | 否 | `now - 86400` (前推 24h) | 起始时间戳。支持 RFC3339 字符串或 Unix Epoch 秒 |
  | `to` | string/int | 否 | `now` (请求发起时刻) | 结束时间戳。支持 RFC3339 或 Unix Epoch 秒 |
  | `media` | string | 否 | `all` | 媒体过滤：`none` \| `image` \| `video` \| `all` |
  | `media_chats` | string | 否 | 空 (全选) | 逗号分隔的会话 ID，仅这些会话注入媒体对象 |
  | `ocr` | string | 否 | `head` | OCR 投影开关：`none` \| `head` \| `full` |
  | `limit` | int | 否 | `0` (不限) | 本次响应最多返回消息条数 (>=0)，触发截断时返回 `next_cursor` |
  | `cursor` | string | 否 | 空 | base64url 编码游标续传，从该条之后推进 |
  | `format` | string | 否 | `json` | 传输格式：`json` \| `ndjson` (分块流式) |
#### (3) `GET /api/health/briefing`
- **功能**：返回家服每日巡检简报快照（`linux1` 09:00 生成、同时推送 Telegram/飞书的那份原文 + 结构化健康指标），供 AI 助手每日拉取监测。
- **鉴权**：**必须** `Authorization: Bearer <TOKEN>`，无 token 返回 `401`（与 `/api/wechat/export` 完全一致）。该端点不匿名开放。
- **数据来源**：只读 `/var/lib/homelab-inspector/briefing.json`（由 `generate-report.py` 在渲染简报时原子写入，可用 `HOMELAB_BRIEFING_PATH` 覆盖）。**不触发采集、不改写内容**；快照缺失时返回 `503`。
- **响应体**：
```json
{
  "generated_at": "2026-10-17T09:01:02+08:00",
  "host": "linux1",
  "status": "green",
  "markdown": "🟢 【家服巡检简报】全绿健康\n…",
  "metrics": {
    "load_1": 0.2, "load_5": 0.31, "load_15": 0.29,
    "memory_used_bytes": 4939212390, "memory_total_bytes": 13958643712,
    "memory_available_bytes": 9126805504,
    "disk_used_pct": 35, "cpu_temp_c": 49.5,
    "docker_running": 16, "docker_total": 16,
    "systemd_failed": 0, "clash_active": true,
    "edgetunnel_available_nodes": 64, "gcp_reachable": true,
    "nearest_asset_days_left": 89, "nearest_asset_name": "…"
  },
  "alerts": []
}
```
- **`status` 语义**：`red` = 硬故障（systemd 失败单元 / Clash 非 active / Docker 未全起 / GCP 不可达 / 磁盘 >90%）；`yellow` = 预警（磁盘 >80% / CPU >75°C / 可用内存 <1Gi / EdgeTunnel 可用节点 <30 / 资产或域名 30 天内到期）；其余 `green`。`alerts` 仅在非 `green` 时非空，每条为一句可读异常描述。

#### (4) `GET /api/wechat/media`
- **功能**：图片检索端点（支持按 OCR 文本、会话、时间、画质过滤，并按需内联上下文）。
- **鉴权**：**必须** `Authorization: Bearer <TOKEN>`，无 token 返回 `401`。
- **安全边界**：**强制套用 blocklist**，被屏蔽的会话内容绝不出现在检索结果中。
- **请求参数**：
  | 参数名 | 类型 | 必填 | 默认值 | 说明与示例 |
  | :--- | :--- | :--- | :--- | :--- |
  | `q` | string | 否 | `""` | OCR 文本不区分大小写的子串匹配 |
  | `chat_id` | string | 否 | `""` | 精确匹配会话 ID（如 `xxx@chatroom` 或 `wxid_xxx`） |
  | `kind` | string | 否 | `image,video` | 媒体类型过滤：`image` \| `video` \| `image,video` |
  | `since` | string/int | 否 | `0` | 起始时间（支持 RFC3339 或 Epoch 秒） |
  | `until` | string/int | 否 | 无穷大 | 截止时间（支持 RFC3339 或 Epoch 秒） |
  | `quality` | string | 否 | `""` | 画质过滤：`hd` / `full` / `thumb` / `bubble` |
  | `limit` | int | 否 | 20 | 单页大小（`include=context` 时最大保护性上限降为 50，其余模式最大 100） |
  | `offset` | int | 否 | 0 | 分页偏移量 |
  | `include` | string | 否 | `""` | 若传入 `context`，为每项内联消息前后的紧邻上下文 |
- **上下文规则（`include=context`）**：
  - 必须限制在同 `chat_id` 内，按 `local_id`/`ts` 获取紧邻的最多 2 条前序 (`before`) 和最多 2 条后序 (`after`) 消息。
  - 上下文消息 `text` 超过 200 字符时截断并追加 `…`。
  - 不含媒体字段以避免递归膨胀。
- **响应体示例**：
```json
{
  "items": [
    {
      "asset_id": "e4f6050b1f3c3a72b0c3d9a1e8f20b41",
      "kind": "image",
      "quality": "hd",
      "format": "jpg",
      "width": 1848,
      "height": 4096,
      "bytes": 2753317,
      "path": "media/e4f6050b1f3c3a72b0c3d9a1e8f20b41/image.jpg",
      "url": "/api/wechat/media/e4f6050b1f3c3a72b0c3d9a1e8f20b41/image.jpg",
      "anchor": {
        "chat_id": "23984719283@chatroom",
        "chat_name": "技术研发交流群",
        "local_id": 61,
        "ts": 1759123456,
        "sender": "wxid_sender123"
      },
      "source": {
        "dat": "msg/attach/hash/2026-10/Img/e4f6050b1f3c3a72b0c3d9a1e8f20b41_h.dat",
        "variant": "_h"
      },
      "ocr": {
        "status": "ready",
        "text": "测试发票开具明细...",
        "provider": "rapidocr",
        "lang": "chi_sim+eng",
        "confidence": 0.93,
        "version": 1,
        "sha256": "4b227777d4dd1fc61c6f884f48641d02b4d121d3fd328cb08b5531fcacdabf8a"
      },
      "context": {
        "before": [
          {"ts": "2026-10-13T07:44:00+08:00", "sender": "张三", "text": "发票我发出来了，大家核对一下"}
        ],
        "after": [
          {"ts": "2026-10-13T07:46:12+08:00", "sender": "李四", "text": "收到，金额无误"}
        ]
      }
    }
  ],
  "total": 1,
  "limit": 20,
  "offset": 0
}
```

#### (5) `GET /api/wechat/media/<asset_id>/<filename>`
- **功能**：媒体（图片 / 视频 / 缩略图）二进制文件下载。
- **鉴权**：**必须** `Authorization: Bearer <TOKEN>`，无 token 返回 `401`。
- **安全防护**：
  - 必须断言通过 `resolve()` 后的文件路径严格位于 `vault/media/` 目录下（403 阻断路径穿越）。
  - `asset_id` 必须严格符合 `^[0-9a-f]{32}$`。
  - `filename` 扩展名必须属于白名单（`ALLOWED_MEDIA_EXTS`：`jpg` / `jpeg` / `png` / `gif` / `webp` / `mp4` / `mov`）。
  - 若该资源所属会话全部被 `blocklist` 屏蔽，拒绝访问（返回 404）。
- **缓存与流式语义**：
  - 响应头返回 `ETag: "<sha256>"`，支持请求头 `If-None-Match` 返回 `304 Not Modified`。
  - 支持 HTTP `Range` 头，支持播放器及 ffmpeg 206 Partial Content 分片拉取（返回 `Content-Range` 与 `Accept-Ranges: bytes`）。

## 3. 标准快照 Payload 规范 (Standard Snapshot Schema)

### 3.1 真实数据结构示例 (脱敏)

```json
{
  "exported_at": "2026-10-13T08:15:00+08:00",
  "range": {
    "from": "2026-10-12T08:15:00+08:00",
    "to": "2026-10-13T08:15:00+08:00"
  },
  "watermark": "2026-10-13T08:00:06+08:00",
  "chats": [
    {
      "name": "技术研发交流群",
      "kind": "group",
      "chat_id": "23984719283@chatroom",
      "display_name": "技术研发交流群",
      "is_group": true,
      "messages": [
        {
          "ts": "2026-10-13T07:45:10+08:00",
          "sender": "wxid_dev_alice",
          "kind": "text",
          "text": "早安！今天的服务发布请注意先检查 Tailscale 节点连通性。",
          "note": ""
        },
        {
          "ts": "2026-10-13T07:50:22+08:00",
          "sender": "wxid_dev_bob",
          "kind": "image",
          "text": "",
          "note": "图片",
          "media": {
            "asset_id": "e4f6050b1f3c3a72b0c3d9a1e8f20b41",
            "kind": "image",
            "quality": "hd",
            "format": "jpg",
            "width": 1848,
            "height": 4096,
            "bytes": 2753317,
            "path": "media/e4f6050b1f3c3a72b0c3d9a1e8f20b41/image.jpg",
            "url": "/api/wechat/media/e4f6050b1f3c3a72b0c3d9a1e8f20b41/image.jpg",
            "anchor": {
              "chat_id": "23984719283@chatroom",
              "chat_name": "技术研发交流群",
              "local_id": 61,
              "ts": 1759123456,
              "sender": "wxid_dev_bob"
            },
            "source": {
              "dat": "msg/attach/hash/2026-10/Img/e4f6050b1f3c3a72b0c3d9a1e8f20b41_h.dat",
              "variant": "_h"
            },
            "ocr": {
              "status": "ready",
              "text": "测试发票开具明细...",
              "provider": "rapidocr",
              "lang": "chi_sim+eng",
              "confidence": 0.93,
              "version": 1,
              "sha256": "4b227777d4dd1fc61c6f884f48641d02b4d121d3fd328cb08b5531fcacdabf8a"
            }
          }
        }
      ]
    },
    {
      "name": "李工",
      "kind": "single",
      "chat_id": "wxid_arch_li",
      "display_name": "李工",
      "is_group": false,
      "messages": [
        {
          "ts": "2026-10-13T08:00:06+08:00",
          "sender": "wxid_arch_li",
          "kind": "file",
          "text": "<msg><appmsg><title>架构演进方案_v2.pdf</title><type>6</type></appmsg></msg>",
          "note": "卡片/链接/文件"
        }
      ]
    }
  ],
  "manifest": {
    "sha256": "3758ab9511af67ada30182739c54e54fc7007da3e6dd138692106d34a96de999",
    "message_count": 3
  }
}
```

### 3.2 字段定义表

#### 顶层字段
| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `exported_at` | string (RFC3339) | 导出快照生成的时间点（服务器上海时区） |
| `range.from` | string (RFC3339) | 实际查询范围起始时间点 |
| `range.to` | string (RFC3339) | 实际查询范围结束时间点 |
| `watermark` | string (RFC3339) | **增量同步游标**。通常为本批次最大 `ts`；若无新消息则退化为 `range.to`。**v1.1**：启用媒体富化且存在未就绪媒体时，会**回退**到该消息之前（见顶部语义变更 #2），消费端不得假设其单调递增 |
| `chats` | array | 按会话聚合的消息数组 |
| `manifest.sha256` | string (hex, 64) | 依据规范化 JSON 规则计算的 `chats` 数组 SHA-256 校验和 |
| `manifest.message_count` | int | 整个快照包含的消息物理总条数 |

#### 会话层字段 (`chats[]`)
| 字段 | 类型 | 规范属性 | 说明 |
| :--- | :--- | :--- | :--- |
| `name` | string | **核心基础** | 会话展示名称（群名、好友备注名；若为空则自动回退为 `chat_id`） |
| `kind` | string | **核心基础** | 会话类型枚举：`"single"`（私聊 / 单聊）或 `"group"`（群聊） |
| `chat_id` | string | *手机超集* | 微信原始会话标识符（如 `wxid_xxx` 或 `xxx@chatroom`） |
| `display_name`| string | *手机超集* | 原始展示名称（未经 fallback 处理） |
| `is_group` | boolean | *手机超集* | 是否为群聊布尔标记 |
| `messages` | array | **核心基础** | 该会话内按发送时间递增排序的消息列表 |

#### 消息层字段 (`chats[].messages[]`)
| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `ts` | string (RFC3339) | 消息创建/接收时间戳，精确至秒，带时区偏移 |
| `sender` | string | 发送者身份。私聊为发言人标识；群聊为发言人 `wxid` 或展示昵称；本机发送为 `"我"` 或 `"self"` |
| `kind` | string | 归一化消息类型枚举：`"text"`, `"image"`, `"voice"`, `"video"`, `"file"`, `"link"`, `"other"` |
| `text` | string | 纯文本内容或卡片/文件/链接的原始 XML 内容；媒体类消息在**基线模式**下为空字符串。**v1.1**：启用媒体富化后，语音会填入转写文本、文件会在原 XML 之后追加解析出的 Markdown（见顶部语义变更 #1）。**不得作为去重键** |
| `note` | string | 消息类型的辅助注释标签（如 `"图片"`, `"语音"`, `"卡片/链接/文件"`） |

---

## 4. 完整性校验：Canonical JSON 与 SHA-256 (Integrity Algorithm)

为保证在不同语言（Python, Go, Node.js, Rust）实现下校验和完全一致，`manifest.sha256` 采用确定性规范化 JSON 序列化规范：

1. **目标对象**：仅对 `chats` 数组（即 `payload["chats"]`）进行摘要计算。
2. **序列化规则**：
   - 字典/对象键名按 Unicode 码点**升序排序** (`sort_keys=True`)。
   - 紧凑分隔符：键值对之间无多余空格，元素之间仅用单逗号分隔（`separators=(",", ":")`）。
   - 保留原生 UTF-8 字符，严禁转义非 ASCII 字符（`ensure_ascii=False`）。
   - 禁用 HTML 实体转义（如 `<` 不得转义为 `\u003c`）。
3. **哈希计算**：将上述生成的 UTF-8 字节流送入标准 SHA-256 计算引擎，输出 64 位小写十六进制文本。

### 各语言实现参考

#### Python
```python
import hashlib
import json

def verify_manifest(payload: dict) -> bool:
    chats = payload.get("chats", [])
    expected_hash = payload.get("manifest", {}).get("sha256", "")
    
    canonical_bytes = json.dumps(
        chats,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":")
    ).encode("utf-8")
    
    actual_hash = hashlib.sha256(canonical_bytes).hexdigest()
    return actual_hash == expected_hash
```

#### Go
```go
func VerifyManifest(chats []ExportChatItem, expectedSha string) bool {
    b, err := json.Marshal(chats)
    if err != nil {
        return false
    }
    var generic any
    _ = json.Unmarshal(b, &generic)

    var buf bytes.Buffer
    enc := json.NewEncoder(&buf)
    enc.SetEscapeHTML(false)
    _ = enc.Encode(generic)
    
    res := buf.Bytes()
    if len(res) > 0 && res[len(res)-1] == '\n' {
        res = res[:len(res)-1]
    }
    hasher := sha256.New()
    hasher.Write(res)
    actual := hex.EncodeToString(hasher.Sum(nil))
    return subtle.ConstantTimeCompare([]byte(actual), []byte(expectedSha)) == 1
}
```

*边界基准：当 `chats` 为空数组 `[]` 时，标准的 SHA-256 值为：*  
`4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945`

---

## 5. 增量消费算法伪代码 (Incremental Consumption Algorithm)

消费端（例如 Muse 早报轮询器、知识库同步 Daemon、Webhook 转发器）应当遵循以下标准循环算法：

```text
Algorithm IncrementalWeChatSync:
    Input: EndpointURL, BearerToken, StorageState (watermark_cursor, seen_sha256_set)
    Output: Processed messages stream

    1. Load cursor from local persistent storage:
       watermark = StorageState.get("last_watermark", default=NOW() - 24_HOURS)
       
    2. Build request:
       URL = EndpointURL + "/api/wechat/export?since=" + UrlEncode(watermark)
       Headers = { "Authorization": "Bearer " + BearerToken }
       
    3. Execute HTTP GET with exponential backoff retry:
       Response = HTTP_GET(URL, Headers)
       If Response.Status != 200:
           HandleError(Response)
           Wait(RetryDelay)
           Return
           
    4. Parse Payload:
       Payload = JSON_DECODE(Response.Body)
       
    5. Integrity Check:
       CalculatedSHA = CanonicalSHA256(Payload.chats)
       If CalculatedSHA != Payload.manifest.sha256:
           Raise IntegrityError("Checksum mismatch! Data corrupted in transit.")
           
    6. Deduplication & Ingestion:
       If Payload.manifest.sha256 in StorageState.seen_sha256_set:
           // Quiet batch; no newly written chats despite watermark tick
           AdvanceCursor(Payload.watermark)
           Return
           
       For each Chat in Payload.chats:
           For each Msg in Chat.messages:
               // Compose unique message dedupe identity
               // v1.1: 去重键不含 Msg.text（text 会随媒体富化而变化）
               MsgUID = SHA256(Chat.name + "|" + Msg.ts + "|" + Msg.sender)
               If MsgUID not in StorageState.seen_messages:
                   IngestIntoAssistant(Chat, Msg)
                   StorageState.seen_messages.add(MsgUID)
                   
    7. Commit State Atomically:
       StorageState.seen_sha256_set.add(Payload.manifest.sha256)
       StorageState.set("last_watermark", Payload.watermark)
       StorageState.persist()
```

---

## 6. 双端实现差异与对齐状态 (Phone vs Homelab Divergence)

在对接消费端前，请明确掌握手机端 (`wx-pad-exporter`) 与家服端 (`wechat-export-api`) 的技术差异。系统在接口层已完成协议归一，但在数据源特征上存在如下已知事实：

| 维度 | 家服 PC 微信端点 (`linux`) | 手机 Pad 微信端点 (`wx_daemon`) |
| :--- | :--- | :--- |
| **数据捕获机理** | 周期性热解密桌面微信本地 SQLite 数据库（WAL 帧内存校验合并） | Xposed 模块在微信进程内实时拦截 WCDB 插入，UDS 串流落库 |
| **历史数据厚度** | 拥有**全量历史与群聊归档**（支持数月甚至数年前的冷数据回溯） | 仅能捕获**该设备安装并启动 Hook 以来实时接收到的新消息**，无历史冷库回溯能力 |
| **发送者标识 (sender)** | 经过联系人反查解析后的**展示名称**（群聊为昵称，私聊为对方备注，本机为 `"我"`） | 原始微信 ID（如 `wxid_xxx` 或 `self` / `"我"`） |
| **会话层结构** | 仅含核心 3 字段：`name`, `kind`, `messages` | **超集**：除上述 3 字段外，扩展提供 `chat_id`, `display_name`, `is_group` |
| **鉴权默认值** | **强制 Bearer Token** 鉴权 | **强制 Bearer Token** 鉴权（默认失败关闭：无 token 且未显式 `--insecure-no-auth` 时服务拒绝启动） |
| **两端数据交集** | 实测两端消息重叠度为 0%（各自独立捕获所属设备接收的消息流），为**互补拓扑**而非互为主备 | 同左 |
