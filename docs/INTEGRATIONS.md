# 微信数据导出集成指南 (Integrations & Downstream Recipes)

> **版本**：`v1.0.2` (2026-10-13)
> **基础契约**：参见 [`docs/DATA_CONTRACT.md`](./DATA_CONTRACT.md)  
> **Schema 校验**：参见 [`schema/export.schema.json`](../schema/export.schema.json)

---

## 1. 概述 (Overview)

本指南汇总了将 `wx-pad-exporter`（或家服 `wechat-export-api`）输出的微信聊天流无缝接入外部智能体的标准实践方案。涵盖以下典型集成场景：
1. **Muse.ai 风格智能体 (Poll & Digest / 早报生成器)**：定时轮询时间切片数据、校验防篡改签名并生成晨间简报。
2. **Grok / 聊天机器人风格 (Webhook Push / 事件流驱动)**：基于 `clients/assistant_webhook.py` 向 Dify、飞书机器人或通用 OpenAI 格式 Webhook 实时分发。
> ⚠️ **去重键必须稳定（v1.1）**：启用媒体富化后，同一消息的 `text` 会随解析完成而变化，
> `watermark` 也可能回退。因此**严禁**把 `text` 用作去重键或缓存键，也不要假设 `watermark` 单调递增。
> 推荐键：`SHA256(chat + "|" + ts + "|" + sender)`。详见 [`DATA_CONTRACT.md`](DATA_CONTRACT.md) v1.1。

3. **通用编程智能体 (Coding Agent / OMP / Claude Code / Codex)**：智能体在开发或排障会话中通过命令行直接查证微信数据。

---

## 2. 方案一：Muse.ai 风格晨间早报智能体 (Poll & Digest Recipe)

### 2.1 架构原理
家服体系中运行的 Muse AI 早报智能体遵循**低频轮询 + 单调水印增量 + SHA-256 防重**模型：
- **触发时机**：每日早间定时触发（如 07:30 CST）。
- **查询范围**：传入 `?since=<YESTERDAY_WATERMARK>`，拉取自上次早报发布以来的增量消息。
- **校验排重**：校验 `manifest.sha256`。若摘要已存在，说明处于静默窗口，无需重复总结。
- **上下文组装**：将各群聊的最新文本和 AppMsg 文件/链接摘要提取为提示词上下文，投递至大语言模型生成晨读早报。

### 2.2 环境变量配置
```bash
# 目标端点地址（Tailscale 私网或本地 ADB 端口转发）
export WECHAT_EXPORT_URL="http://127.0.0.1:8765/api/wechat/export"
# Bearer Token（若端点开启了鉴权）
export WECHAT_BEARER_TOKEN="your_secure_bearer_token"
# 增量游标持久化文件
export WECHAT_STATE_FILE="$HOME/.local/share/wechat-vault/state/muse_cursor.json"
```

### 2.3 生产级拉取与处理脚本 (`clients/fetch_export.py`)
项目内置的 [`clients/fetch_export.py`](../clients/fetch_export.py) 已原生实现超时指数退避、Tailscale 代理绕过及水印持久化。

直接运行命令：
```bash
python3 clients/fetch_export.py \
  --url "${WECHAT_EXPORT_URL}" \
  --token "${WECHAT_BEARER_TOKEN}" \
  --state-file "${WECHAT_STATE_FILE}" \
  --output "/tmp/morning_brief_input.json"
```

运行输出示例：
```text
[INFO] Loaded watermark: 2026-10-12T07:30:00+08:00
[INFO] Successfully fetched snapshot. Chats: 12, Total Messages: 158
[INFO] Manifest SHA-256 verified: 3758ab9511af67ada30182739c54e54fc7007da3e6dd138692106d34a96de999
[INFO] Updated watermark to: 2026-10-13T07:28:15+08:00
```

---

## 3. 方案二：Grok-bot / 聊天机器人风格实时 Webhook 推送 (Push Recipe)

基于 [`clients/assistant_webhook.py`](../clients/assistant_webhook.py)，可实现近实时的微信消息转发展示与多端联动。

### 3.1 飞书群自定义机器人 (Feishu Bot)
将指定群聊的新消息格式化为飞书卡片推送到团队运营群。

#### 环境变量
```bash
export FEISHU_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

#### 启动指令
```bash
python3 clients/assistant_webhook.py \
  --export-url "http://127.0.0.1:8765/api/wechat/export" \
  --webhook-url "${FEISHU_WEBHOOK_URL}" \
  --webhook-type "feishu" \
  --poll-interval 15 \
  --state-file "/tmp/feishu_bot_cursor.json"
```

### 3.2 Dify 工作流触发 (Dify Workflow)
将微信入站消息作为 Dify 知识库或问答 Agent 的触发源。

#### 环境变量
```bash
export DIFY_API_KEY="app-xxxxxxxxxxxxxxxxxxxxxxxx"
export DIFY_WEBHOOK_URL="https://api.dify.ai/v1/workflows/run"
```

#### 启动指令
```bash
python3 clients/assistant_webhook.py \
  --export-url "http://127.0.0.1:8765/api/wechat/export" \
  --webhook-url "${DIFY_WEBHOOK_URL}" \
  --webhook-type "dify" \
  --poll-interval 10
```

### 3.3 通用 OpenAI / 自定义 HTTP 接收端
投递标准 JSON 载荷（`event: "wechat_message"`, `sender`, `content`, `chat_id`）。

```bash
python3 clients/assistant_webhook.py \
  --export-url "http://127.0.0.1:8765/api/wechat/export" \
  --webhook-url "http://127.0.0.1:9000/webhook/wechat" \
  --webhook-type "custom"
```

---

## 4. 方案三：通用编码智能体消费模式 (Coding Agent: omp / Claude / Codex)

当 AI 工程师或运维智能体（如 `omp`）需要排查或汇总微信数据时，推荐遵循**标准探测三部曲**：

### 步骤 1：健康探针与网络连通性检查
```bash
# 验证 ADB 转发或私网接口可用性
curl -sS --fail http://127.0.0.1:8765/health | jq '{status, total_messages, uds_connected}'
```

### 步骤 2：拉取并执行本地 JSON Schema 验证
```bash
# 拉取最新切片并用 Python 严格校验 Schema
curl -sS -H "Authorization: Bearer ${TOKEN}" "http://127.0.0.1:8765/api/wechat/export?since=$(date -v-1H +%s)" > /tmp/latest_slice.json

python3 -c "
import json, jsonschema
schema = json.load(open('schema/export.schema.json'))
data = json.load(open('/tmp/latest_slice.json'))
jsonschema.validate(instance=data, schema=schema)
print('✅ Snapshot conforms strictly to export.schema.json')
"
```

### 步骤 3：智能体上下文提炼 (提取群聊核心摘要)
```bash
# 仅提取群聊名称和文本行，供智能体理解背景，降低 Token 消耗
jq -r '.chats[] | select(.kind=="group") | "=== " + .name + " ===\n" + (.messages[] | select(.kind=="text") | .sender + ": " + .text)' /tmp/latest_slice.json
```

---

## 5. 方案四：Muse 消费端接入与验证 (Muse Consumption & Probing)

微信 4.x 图片消息富化上线后，导出的图片消息携带结构化 `media` 对象及 OCR 文本。消费端既可零改动直接沿用旧接口，也可以深度透传图片元数据实现视觉多模态与会话定位。

### 5.1 接入模式对比

1. **零改动可用 (Zero-touch Compatibility)**：
   - 消费端只需继续读取 `msg["text"]` 或 Webhook 载荷的 `content`。
   - 若图片已完成 OCR，`text` 会直接包含 OCR 截断投影（≤2000 字符）；若无文字或未识别，`content` 仍回退为 `[图片]`。旧版早报与摘要管道完全不破坏。

2. **建议改动 (Full Enriched Media Passthrough)**：
   - 开启 `--include-media`（默认开启），Webhook 载荷中将包含：
     - `media_url`：图片静态相对路径（如 `/api/wechat/media/<asset_id>/image.jpg`）；
     - `media_url_abs`：原图绝对 HTTP 路径（`http(s)://<host>/api/wechat/media/<asset_id>/image.jpg`，由 `--public-base-url` 或导出端点推导）；
     - `media_url_auth`：`"bearer"`（明确指示消费端拉取原图时需在 HTTP Authorization 头中附带 Bearer Token）；
     - `media_asset_id`：图片指纹，用于去重与缓存；
     - `chat_name`：图片所在会话的可读名称（优先使用群聊备注/昵称，缺失时回退 `chat_id`）；
     - `ocr_text`：完整的未截断 OCR 结果（若 OCR 关闭或无文字则不存在）；
     - `media`：完整的富化元数据对象（包含尺寸、格式、`anchor` 锚点和 `source` 来源）。

> 💡 **核心原则：Muse 不需要导出侧 OCR**  
> Muse 消费端自带多模态视觉能力（如 GPT-4o / Claude Vision / Gemini），直接拿到 `media_url_abs` 即可自行理解图片画面与图内文字。导出侧的本地 OCR 仅供没有视觉模型的纯文本下游兜底。
> **何时取图**：凡是 `media.kind == "image"` 且存在 `media_url_abs` 即可直接拉取原图进行多模态分析；若 `ocr.status == "empty"`，代表本地无文字提取，多模态模型更应接入解读。
### 5.2 三步验证法 (Three-Step Verification)

项目内置探针工具 `clients/muse_probe.py`（零第三方依赖，纯标准库），提供可重复的闭环验证：

#### 第一步：数据层自检 (`--audit`)
检查当前导出数据中图片消息的 OCR 状态与会话锚点（支持读取本地 JSON 或直接连导出端点）：

```bash
python3 clients/muse_probe.py --audit --input /tmp/wechat_export.json
# 或直接打在线端点
# python3 clients/muse_probe.py --audit --endpoint "http://127.0.0.1:8765/api/wechat/export" --token "${WECHAT_BEARER_TOKEN}"
```

**期望输出示例**：
```text
=== WeChat Image Media Enrichment Audit ===
Total Image Messages Detected: 25

Metric                               | Count    | Ratio     
------------------------------------------------------------
Total Images                         | 25       | 100.0%
Text Non-empty                       | 23       | 92.0%
Media Object Present                 | 25       | 100.0%
Anchor Complete (5 fields)           | 25       | 100.0%
Chat Name Resolved (!= chat_id)      | 22       | 88.0%

OCR Status Distribution:
  - empty          : 2      (8.0%)
  - ready          : 23     (92.0%)

Sample Images (up to 5, masked):
#   | Chat Name            | Timestamp                | OCR Preview (≤80 chars)                 
--------------------------------------------------------------------------------------------
1   | 技术研发交流群       | 1759123456               | 解约协议与家庭医生签约通知全文...       
2   | 社区医疗互助群       | 1759123480               | 门诊预约缴费凭证 手机号*** 经办人李四   

AUDIT: PASS (text_rate=92.0%[>=80%], anchor=25/25[=100%], ocr_ready_empty=100.0%[>=90%])
```

#### 第二步：载荷预览 (`--dry-run`)
在不发送任何网络请求、无需真实 Token 的情况下，预览探针将要构建的上下文与评测载荷：

```bash
python3 clients/muse_probe.py --dry-run --input /tmp/wechat_export.json --limit 1
```

**期望输出示例**：
```text
[DRY-RUN] Found 25 image messages. Showing up to 1 sample payloads:

--- Payload Sample 1/1 (asset_id=3a7b9c...) ---
{
  "messages": [
    {
      "role": "user",
      "content": "WeChat Message Context:\n```json\n{\n  \"kind\": \"image\",\n  \"media\": {\n    \"asset_id\": \"3a7b9c...\",\n    \"anchor\": {\n      \"chat_name\": \"技术研发交流群\"\n    }\n  }\n}\n```\n\nQuestion: 这张图里说了什么？前后在聊什么？\nPlease answer based strictly on the message context and its media enrichment information."
    }
  ],
  "temperature": 0.0,
  "max_tokens": 512
}
```

#### 第三步：语义层打分 (`--score`)
通过探针集 `probes.json`，把带图会话上下文投递到待测 AI 助手 Webhook，验证模型理解的准确度并严格防范幻觉。

> ⚠️ **定位与边界（重要契约）**：  
> `--score` 属于 **Prompt 构造回归与富化元数据问答契约测试，不验证视觉理解**。`--score` 仅校验探针集自身可用、Schema 结构合法，以及测试下游 Agent 能否基于消息富化文本（如导出侧本地 OCR 文本 `ocr.text` 与会话锚点 `anchor`）按关键词如实回答且不产生幻觉。  
> **真正验证视觉理解必须使用 `--vision` 输出的多模态载荷 + 真实解密图片/视频海报 + 具备 Vision 视觉能力的端到端大模型**（详见 5.3 节）。
#### 5.2.1 自建探针集规范与字段定义
探针集文件为 JSON 数组，位于 `probes/probes.json`。每条用例包含以下字段：

| 字段 | 类型 | 必填 | 说明 |
| :--- | :--- | :--- | :--- |
| `id` | `string` | 建议 | 探针用例唯一标识（如 `probe_01_text_table`） |
| `type` | `string` | 建议 | 用例类型标签（`text_table`, `pure_photo`, `chat_context`, `counter_hallucination`, `video_first_frame`） |
| `asset_id` | `string` | **是** | 目标媒体资产 32 位十六进制 MD5 指纹（与导出载荷中的 `media.asset_id` 精确对齐） |
| `question` | `string` | **是** | 驱动待测 AI 模型/Webhook 评测的具体问题 |
| `must_contain` | `string[]` | 否 | 期望回答必须包含的关键词列表；全命中记 `hit`，部分命中记 `partial` |
| `must_not_contain` | `string[]` | 否 | 防幻觉禁止词；回答包含任一词即被判定为幻觉 `hallucination` |
| `expect_empty` | `boolean` | 否 | 是否期望无文字或纯画面回答（默认 `false`）；为 `true` 时执行空内容与幻觉检测 |
| `description` | `string` | 否 | 该条探针的测试意图与验收标准说明 |

##### 5 类典型探针用例设计原则：
1. **文字图（表格/通知/发票单据）**：针对图内文字提问具体字段（如值班人、生效日期），`must_contain` 设为核心提取关键词；
2. **纯照片（风景/静物）**：画面无文字，设置 `expect_empty: true`，并将 `must_not_contain` 设为容易被大模型幻觉编造的事物（如 `"发票"`, `"金额"`, `"身份证号"`）；
3. **群聊上下文**：问题询问"这是哪个群在聊什么/谁发的消息"，验证模型是否正确理解会话锚点（`chat_name`, `sender`, `ts`）；
4. **反例/防幻觉**：故意询问图中不存在的字段（如报销金额、快递单号），期望模型回答"未提及"或"图中没有"，严禁瞎编数值；
5. **视频首帧**：针对视频首帧/海报图提问，验证视频消息的视觉理解链路。

`probes/probes.json` 片段示例：
```json
[
  {
    "id": "probe_01_text_table",
    "type": "text_table",
    "asset_id": "10000000000000000000000000000001",
    "question": "表格中周三夜班的值班人员是谁？值班地点在何处？",
    "must_contain": ["周三", "王工", "主控机房"],
    "must_not_contain": ["发票", "报销"],
    "expect_empty": false,
    "description": "排班表格OCR文字识别与关键词提取"
  },
  {
    "id": "probe_04_photo_landscape",
    "type": "pure_photo",
    "asset_id": "20000000000000000000000000000001",
    "question": "这张户外植物照片中能识别出哪些文字内容？",
    "must_contain": [],
    "must_not_contain": ["发票", "金额", "身份证号", "文字："],
    "expect_empty": true,
    "description": "纯风景照片无文字检测防幻觉"
  }
]
```

#### 5.2.2 质量门禁与退出码参数

探针内置了质量门禁与机器可读输出，便于无缝嵌入 CI/CD 流水线：
- `--fail-under <float>`（默认 `0.8`）：设置综合命中率通过阈值（0.0 ~ 1.0）。综合命中率计算规则为：`composite_rate = (hits + 0.5 * partials) / total`。若综合命中率低于此阈值，命令退出码为 `1`；
- `--require-zero-hallucination`：严格零幻觉门禁。只要产生任意 1 条幻觉命中，立即判定失败并退出码 `1`；
- **机器可读输出行**：在命令输出的最末行固定打印标准单行，格式为：  
  `SCORE: pass|fail hit=<x> partial=<y> hallu=<z> total=<n>`

运行真实评分命令示例：
```bash
python3 clients/muse_probe.py \
  --score \
  --input /tmp/wechat_export.json \
  --expect probes/probes.json \
  --webhook-url "http://127.0.0.1:9000/webhook/wechat" \
  --webhook-type "custom" \
  --fail-under 0.8 \
  --require-zero-hallucination
```

输出示例：
```text
=== Semantic Probe Score Summary ===
Total Probes:       12
Hit Rate:           91.7% (11/12)
Hallucinations:     0
Grounding Accuracy: 100.0%
OVERALL:            PASS

Details:
--- Probe #1 [PASS] (asset=100000000000...) ---
...

SCORE: pass hit=11 partial=1 hallu=0 total=12
```

#### 5.2.3 CI 门禁与离线自检理由

在 GitHub Actions CI 流水线中，采用离线自检模式：
```bash
python3 clients/muse_probe.py --score --expect probes/probes.json --dry-run --fail-under 0.0
```

**为什么 CI 中只做 `--dry-run` 离线自检，而不跑联网真实评分？**
1. **确定性与抗抖动 (Determinism)**：大模型推理输出存在非确定性，外部 Webhook 亦受网络延时影响。将真实打分放入 CI 会导致偶发超时或输出波动，引入不可控的构建抖动（Flaky CI）；
2. **凭据安全与零泄露 (Secret Hygiene)**：离线自检无需注入任何下游 AI 平台的 API Token 或生产 Webhook 凭据，消除 GitHub Actions 环境的密钥外泄风险；
3. **资源成本节约 (Zero Inference Cost)**：日常 Git 提交与 PR 验证频繁，离线校验避免产生不必要的外部 LLM 推理调用与 Token 计费消耗；
4. **结构契约守护 (Schema Gate)**：CI 的核心目标是验证代码质量与静态资产完好性。`--dry-run` 会严格解析 `probes/probes.json`，检查条目数是否达标（≥12 条）、字段结构与格式是否合法，并模拟载荷组装，确保探针集资产不发生意外损坏。端到端语义打分则安排在生产发布前或准入验证环节按需执行。

### 5.3 Muse 多模态视觉接入 (Vision Recipe)

当 Muse 自带视觉大模型时，标准接入流程分为三步：
1. **接收事件**：`assistant_webhook.py` 默认开启 `--include-media`，接收到图片消息事件；
2. **按需拉取原图**：从载荷中读取 `media_url_abs`，若 `media_url_auth == "bearer"`，带上 `Authorization: Bearer <TOKEN>` 发起 HTTP GET 请求获取图片流；
3. **投递多模态模型**：组装包含上下文、发送者、会话名称及原图的 OpenAI 兼容多模态请求。

#### Muse 配置项 ↔ 导出侧字段映射表

| Muse 侧配置项 | 导出侧字段 / 来源 | 典型值 / 说明 |
| :--- | :--- | :--- |
| `wechat_export_endpoint` | `--endpoint` / `--export-url` | `http://127.0.0.1:8765/api/wechat/export` |
| `wechat_bearer_token` | `--token` / `WECHAT_BEARER_TOKEN` | 用于导出 API 与媒体原图下载鉴权 |
| `image_download_url` | 载荷中的 `media_url_abs` | `http://127.0.0.1:8765/api/wechat/media/<asset_id>/image.jpg` |
| `image_auth_scheme` | 载荷中的 `media_url_auth` | `"bearer"`（必须附带 Authorization 请求头） |
| `image_download_timeout` | 客户端下载超时 | 推荐配置为 `10s` ~ `30s` |
| `vision_model` | Muse 多模态模型名称 | 如 `gpt-4o`, `claude-3-5-sonnet-20241022` |
| `ocr_fallback_text` | 载荷中的 `ocr_text` 或 `content` | 可选兜底文本（当多模态模型降级或超时时使用） |

#### 生成 OpenAI 兼容多模态测试载荷 (`--vision`)
探针内置 `--vision` 工具，可直接生成供多模态模型消费的请求体（不发送网络请求）：

```bash
# 1. 生成 base64 内联格式（需指定本地图片目录）
python3 clients/muse_probe.py --vision --input /tmp/wechat_export.json --image-dir /tmp/exports

# 2. 生成外部绝对 URL 格式（适合直接投递支持 URL 拉取的模型）
python3 clients/muse_probe.py --vision --input /tmp/wechat_export.json --no-base64 --public-base-url "http://192.168.1.100:8765"
```


### 5.3.1 视频首帧进多模态与 `--video-mode`

在微信消息流中，视频通常体积较大（MB~GB 级别），直接对视频本体做 base64 内联投递至大语言模型不仅浪费传输带宽，且极易导致上下文窗口溢出或网关超时。

为此，探针 `--vision` 针对视频消息提供了**首帧海报（Poster）多模态投递机制**。通过 `--video-mode {poster,url,both}`（默认 `poster`）控制：

#### 三种 `--video-mode` 载荷差异
| 模式 (`--video-mode`) | Text 部分 | Image/Vision 部分 | 适用场景 |
|:---|:---|:---|:---|
| `poster`（默认） | 说明为视频首帧（含时长）与视频本体 URL（若本地无首帧则提示无首帧需自行抽帧） | 首帧图片（本地存在则 base64 内联 `image_url`） | 默认多模态视觉问答，模型主要理解视频首帧静态信息 |
| `url` | 仅输出 `media_url_abs` 视频下载链接（含时长，提示需 Bearer 鉴权） | 无（不投递 `image_url`） | 纯链接通知、消费端已有外部视频解析器链路 |
| `both` | 说明为首帧与本体 URL，并在尾部追加独立视频链接块 | 首帧图片（本地存在则 base64 内联 `image_url`） | 既需要模型看首帧图，又需要下游 Agent 接收直接视频直链 |

> ⚠️ **视频本体永不 base64**：无论何种模式，视频文件均保持 URL 外部拉取，不内联进 JSON。

#### 首帧与视频内容提示及自行抽帧建议
**首帧 ≠ 完整视频内容**：长视频首帧往往为黑屏、转场过渡或封面片头，信息量有限。若业务对视频全片理解有硬性要求，消费端应根据 `media_url_abs` 携带 Bearer Token 下载视频，并使用 `ffmpeg` 自行抽帧：

```bash
# 从远程 API 下载并按每 2 秒抽取一帧示例
curl -H "Authorization: Bearer <TOKEN>" -o /tmp/video.mp4 "<media_url_abs>"
ffmpeg -i /tmp/video.mp4 -vf "fps=1/2" -q:v 2 /tmp/frame_%03d.jpg
```

#### Muse 早报/多模态集成推荐组合
```bash
python3 clients/muse_probe.py \
  --vision \
  --only video \
  --video-mode poster \
  --input /tmp/wechat_export.json \
  --image-dir /tmp/exports
```
结合 Webhook 导出参数推荐：`--only video --video-mode poster --media=video --ocr=none`。
---

### 5.4 按需拉取与体积控制 (On-demand Slicing & Byte Estimation)

在生产早报或高频巡检中，为了避免多余富化造成网络 IO 膨胀，导出接口支持三种精准过滤参数（`clients/assistant_webhook.py` 已提供原生 CLI 映射）：

1. `--media {none,image,video,all}`（默认 `all`）：控制哪些类型的多媒体对象附带结构化元数据；
2. `--media-chats <id1,id2,...>`：仅在指定的重点群聊（如运营群、通知群）中开启富化；
3. `--ocr {none,head,full}`（默认 `head`）：控制返回的 OCR 文本是截断头投影还是全文。

#### 生产推荐过滤组合
- **Muse 视觉早报推荐组合**（只要原图、不需本地 OCR、限制在重点群）：
  ```bash
  python3 clients/assistant_webhook.py \
    --media image \
    --ocr none \
    --media-chats "1459496660@chatroom,58613413425@chatroom"
  ```
- **纯文本高频巡查组合**（零富化开销，体积最小）：
  ```bash
  python3 clients/assistant_webhook.py \
    --media none \
    --ocr none
  ```

#### 预估载荷体积对比 (`--estimate-bytes`)
可使用探针快速量化不同策略下的 JSON 体积差异：
```bash
python3 clients/muse_probe.py --estimate-bytes --input /tmp/wechat_export.json
```
**实测输出示例**：
```text
=== Payload Size Estimation Across Filter Modes ===
Mode Combination               | Estimated JSON   | Ratio   
------------------------------------------------------------
media=none & ocr=none          | 45.20 KB         | 1.0x
media=image & ocr=head/full    | 98.40 KB         | 2.18x
media=all (image + video)      | 142.10 KB        | 3.14x
```
