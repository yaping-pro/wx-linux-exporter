# 变更日志 (Changelog)

本项目所有显著变更均记录于此文档。
本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/) 语义化版本规范，格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

---

## [Unreleased]

---

## [1.5.0] - 2026-10-27
> 包版本 1.5.0 / 数据契约版本 1.3.1（契约本身未升版，本次为多模态/探针门禁/发布工程能力发布）

### Added
- **视频首帧投递多模态与 Webhook 透传 (`clients/muse_probe.py`, `clients/assistant_webhook.py`)**：
  - **视频首帧海报投递 (`--video-mode poster|url|both`)**：
    - 默认 `poster` 模式提取视频首帧 (`thumb.jpg`) 并以 `image_url` base64 形式投递至多模态大模型；
    - 视频本体永不内联传输（避免传输膨胀与网关超时）；
    - `locate_local_poster_file` 支持 `exports/media/<date>/<asset_id>/thumb.jpg` 与 `vault/media/<asset_id>/thumb.jpg` 路径解析；
    - 无本地 poster 时降级为 url 并在文本中提示消费端自行抽帧。
  - **Webhook 增强**：
    - `custom` 与 `dify` 载荷支持输出 `media_poster_url` 与 `media_poster_url_abs`；
    - `feishu` 消息卡片支持追加视频首帧图片行；
    - 纯图片消息载荷签名与兼容性保持 100% 不变。
- **语义探针回归门禁与自检套件 (`probes/probes.json`, `tests/test_muse_score.py`)**：
  - **12 条基准用例集**：全面覆盖文字图 (`text_table`, `text_notice`, `text_receipt`)、纯照片 (`pure_photo`)、群聊上下文 (`chat_context`)、反幻觉 (`counter_hallucination`) 与视频首帧 (`video_first_frame`) 五类真实场景；
  - **硬门禁机制**：`muse_probe.py --score` 新增 `--fail-under`（最小 composite 准确率）与 `--require-zero-hallucination`（幻觉零容忍）强校验参数；
  - **离线自检与输出**：`--score` 支持 `--dry-run` 离线自检，输出标准机器可读行 `SCORE: pass hit=12 partial=0 hallu=0 total=12`；
  - **CI 持续集成守护**：在 GitHub Actions CI 工作流中新增 `Probe suite self-check` job，保障探针规范与打分引擎不退化。
- **CI/CD 自动化多架构 Docker 镜像发布与 Compose 编排优化 (`.github/workflows/docker-publish.yml`, `docker-compose.yml`)**：
  - **GHCR 多架构发布**：通过 GitHub Actions 自动交叉编译 `linux/amd64` 与 `linux/arm64` 镜像并推送到 GitHub Container Registry (`ghcr.io/yaping-pro/wx-linux-exporter`)；
  - **清单强校验**：构建后通过 `docker buildx imagetools inspect` 确认双架构层就绪；
  - **Compose 默认镜像**：`docker-compose.yml` 默认拉取 GHCR 预构建镜像，保留 `build:` 注释块供本地二次开发。
- **文档更新**：
  - `docs/INTEGRATIONS.md`：详细阐述 5.2 探针集用例规范与 5.3 视频首帧多模态投递机制；
  - `docs/DOCKER.md`：新增「镜像来源与更新 (GHCR)」章节，详解多架构支持、手动拉取、平滑升级路径、版本锁定及本地构建回退；
  - `README.md`：在 Docker 部署小节补充 `docker pull ghcr.io/yaping-pro/wx-linux-exporter:latest`。

## [1.4.0] - 2026-10-27
> 包版本 1.4.0 / 数据契约版本 1.3.1（契约本身未升版，本次为能力性发布）

### Added
- **分页 + 流式输出 + OpenAPI 3.1 契约规范（契约 v1.3.1，纯增量）**：
  - **游标分页（`/api/wechat/export`）**：
    - `limit`（非负整数，默认 `0` = 不限）：单次最多返回消息数，消息跨全部允许会话严格按 `(create_time, chat_id, local_id)` 升序推进；
    - `cursor`（base64url 编码）：从指定位置之后继续；格式不符或非法时返回 `400 {"error":{"code":"invalid_cursor",...}}`；
    - 响应根新增 `next_cursor`（有后续给出，否则 `null`；`limit=0` 恒为 `null`）；`payload_stats` 补充 `truncated: true|false`；
    - `watermark` 水位语义保证：有 `next_cursor`（截断）时不提前推进水位（保持 `since`），仅在全部已结算（`next_cursor` 为 `null`）时推进。
  - **流式分块输出（`format=ndjson`）**：
    - 支持 `format=ndjson`（默认 `json`），输出 `Content-Type: application/x-ndjson`；
    - 采用分块传输编码（`Transfer-Encoding: chunked`），边生成边写，行序列严格为 `manifest` 首行、`chat` 行、`message` 行、`end` 末行；逐字段与 JSON 模式保持一致；
    - 与 `limit`、`cursor`、`media`、`ocr` 完全兼容。
  - **OpenAPI 3.1 规范**：
    - 新建 `docs/openapi.yaml`，实写覆盖 `/api/wechat/export`、`/api/wechat/media`、`/api/wechat/media/{asset_id}/{filename}` 与 `/api/health/briefing`；包括 BearerAuth 鉴权、HTTP 206 Range 分片、`media`/`ocr` 枚举与 `next_cursor`/`payload_stats` 字段。
  - **测试与文档**：
    - 新增 `tests/test_api_paging.py`，完整覆盖 limit 截断拼接、cursor 解析/400、非法 limit 400、ndjson 行序与协议头、分页水位不提前推进；
    - 更新 `docs/DATA_CONTRACT.md` 升至 v1.3.1，声明纯增量兼容。
- **三个正交过滤开关 + 视频注入 + 体积统计（契约 v1.3.0）**：
  - **三个正交查询开关（`/api/wechat/export`）**：
    - `media ∈ none|image|video|all`（默认 `all`）：按需控制媒体对象注入，未选中媒体消息保留 `note`（「图片」「视频」），消费端可按需事后拉取；
    - `media_chats`（逗号分隔 chat_id，默认全选）：仅指定会话注入 `media`，其余会话优雅降级为 `note`；
    - `ocr ∈ none|head|full`（默认 `head`）：`none` 彻底移除 OCR 与投影，`head` 仅 `text` 投影，`full` 保留 `text` 投影与 `media.ocr.text` 全文。
    - 针对新增参数引入结构化错误响应 `400 {"error":{"code":"invalid_param","message":"...","hint":"..."}}`，保持既有错误响应形态不变。
  - **视频富化注入与媒体服务扩展**：
    - 就绪视频消息注入结构化 `media` 对象（含 `asset_id`、`duration_s`、`poster`、`format`、`bytes`、`width`、`height`、`anchor`、`source`）；
    - `media_note(asset, kind)` 泛化：统一处理图片与视频未就绪/跳过状态映射（`视频（处理中）`、`视频（未下载）`、`视频（超大）`、`视频（解析失败）`）；
    - `ALLOWED_IMAGE_EXTS` 重命名为 `ALLOWED_MEDIA_EXTS`，扩展支持 `mp4` 与 `mov`；媒体端点支持视频 `video/mp4` MIME 与 HTTP `Range`（206 Partial Content / `Content-Range`）断点续传播放；
    - `GET /api/wechat/media` 增加 `kind` 筛选（支持 `image`、`video` 或 `image,video`，默认两者），返回结果携带 `duration_s` 与 `poster`。
  - **Payload 体积统计**：导出响应根新增 `payload_stats: {"messages": N, "media_included": N, "ocr_chars": N}`，`ocr=none` 时 `ocr_chars` 强制为 0。
  - `schema/export.schema.json`：同步更新 `mediaItem`（新增 `video` kind、`mp4`/`mov` format、`duration_s`、`poster`）与根对象 `payload_stats`。
  - `docs/DATA_CONTRACT.md`：升级至 v1.3.0 规范，详细描述三个开关语义、视频媒体形状、payload_stats 统计与严格向后兼容性声明。
  - `tests/test_media_filters.py`：新增测试套件，全面覆盖非法参数 400 结构、正交过滤开关、视频 media 形状、media_note 映射、Range 206 分片响应与 `payload_stats` 计数准确性。
- **图片离线解密与 OCR 富化接入（契约 v1.2.0）**：
  - `bin/wechat_media.py`：新增图片消息（`local_type=3`）解析流。通过 `hardlink.db`（`image_hardlink_info_v4`）与磁盘模式探测定位 `.dat` 变体，支持变体画质择优（`_h > full > _t > _b`）、wxgf 动图策略过滤、惰性解密器与本地 OCR 协同、防解压炸弹像素闸、双目录物化（`exports/media/<date>/<asset_id>/` 与 `vault/media/<asset_id>/`）以及基于内容哈希的跨会话幂等复用。
  - `bin/wechat_export_api.py`：
    - `GET /api/wechat/media`：图片检索端点，支持 OCR 文本模糊子串、会话 ID、时间范围与画质筛选；支持 `include=context` 获取紧邻前后各最多 2 条上下文（文本 200 字符截断，限额自动收紧至 50），严格套用 Bearer 鉴权与 `blocklist` 屏蔽会话隔离。
    - `GET /api/wechat/media/<asset_id>/<filename>`：原图二进制下载端点，支持 `Content-Type` 自适应、ETag (sha256) 与 `If-None-Match` 304 缓存协商；路径严格限制在 `vault/media/` 目录下（403 阻断路径穿越），扩展名白名单校验，屏蔽会话资源防探测。
    - `/api/wechat/export`：就绪图片消息附加可选 `media` 对象，`text` 字段填入 `media.ocr.text` 的截断投影（≤2000 字符，未就绪保持空串），无缝兼容仅读取 `text` 的既有消费端；`anchor` 附带 `chat_name` 与原始会话坐标。
  - `schema/export.schema.json`：新增严格定义的 `mediaItem` 与 `anchor` Schema（`additionalProperties: false`），保持向后兼容。
  - `docs/DATA_CONTRACT.md`：新增 v1.2.0 契约章节、检索与原图端点规范、上下文抽取说明与消费端读取指引。
  - `docs/MEDIA_ENRICHMENT.md`：更正第 9 节结论为实机验证事实（18,402 个文件 100% 离线 V2 可解），更新架构与能力边界。
  - `config/media_policy.example.json`：新增 `image` 段配置模板（默认关闭）。
  - `tests/test_media_images.py`：综合测试套件，全面覆盖画质择优、asset_id 跨群去重复用、anchor 精准定位、OCR 三态降级、wxgf 动图跳过、blocklist 全端点拦截、路径穿越防御、304 协商缓存、Schema 严格性、上下文无串味抽取与截断、`chat_name` 智能解析与回退。
---

## [v1.1.1] - 2026-10-16
### Fixed
- **P0 `blocklist` 被静默忽略（隐私/安全面）**：`create_server()` 构造 `server_context` 时漏传 `blocklist`，导致**任何** blocklist 都不生效——被屏蔽的会话会照常出现在导出结果中。由于 `main()` 同样经由 `create_server`，该缺陷对生产部署同样生效（仅因默认 `blocklist: []` 而未暴露）。已补回该键，并新增端到端回归测试 `tests/test_chat_filtering.py`（起真实回环服务断言屏蔽会话被排除、无 token 返回 401）；已反向验证：回退修复则该测试失败。

---

## [v1.1.0] - 2026-10-16

### Added
- **媒体富化层（可选，默认关闭）**：把基线导出中的媒体占位符解析为可读文本并注入既有 `text` 字段，形成「底层解密 → 原子发布 → 只读 API → 应用层可读内容」的完整链条。
  - `bin/wechat_media.py`：独立 worker（三层闸：会话白名单 / 类型 / 预算；md5 去重；日预算硬闸；staging 稳定性校验；幂等 ledger）。
  - `bin/mineru_agent.py`：MinerU 双通道客户端（**agent 免 token** ≤10MB/≤20 页；**precise 需 token** ≤200MB/≤200 页；超页自动切片拼接）。仅标准库 + `curl`，无新增运行时依赖。
  - **语音转写（零成本）**：读取微信客户端自带的语音转文字结果（message 表 `packed_info_data`，protobuf `field5.field2`），无需下载音频、无需 SILK 解码、无需 ASR 模型、无网络请求。
  - `config/media_policy.example.json`：策略模板（默认 `enabled: false`）。
  - `deploy/systemd/wechat-media.{service,timer}`：worker 单元（`LoadCredentialEncrypted` 一行可选的 MinerU 凭据；未启用时为 no-op）。
  - `docs/MEDIA_ENRICHMENT.md`：能力边界、关联算法、凭据配置、`deny_reason` 速查、已知限制。
- `tests/test_wechat_media.py`：23 项离线回归测试（protobuf 转写解析用合成 fixture、通道路由、白名单语义、策略默认值合并、路径解析分支）。

### Changed
- **`docs/DATA_CONTRACT.md` 升级至 `v1.1.0`**，新增三条语义变更声明（启用富化后生效，默认关闭时与 v1.0.x 完全一致）：
  1. `text` 不再是静态原文（语音填入转写、文件追加解析 Markdown）→ **严禁用作去重键**；
  2. `watermark` 不再严格单调（含未就绪媒体时回退，供消费端复拉）；
  3. 富化只作用于白名单会话，可随时收窄。
  同时修正 §5 去重伪代码：`MsgUID` 移除 `text`（`SHA256(chat|ts|sender)`）。
- `schema/export.schema.json`：`text` / `watermark` 描述同步至 v1.1 语义（结构未变，无新增字段、无新增端点、无新增键名）。
- `bin/wechat_export_api.py`：媒体注入 + 水位保持 + 白名单投递闸；**保留并兼容既有 fail-closed 鉴权加固**（`--insecure-no-auth` 等未受影响）。
- `deploy/install-systemd.sh`：纳管 `wechat-media.timer`（默认启用但为 no-op，直到策略显式开启）。
- `.github/workflows/ci.yml`：新增媒体富化回归测试步骤。
- `.gitignore`：忽略媒体富化运行期产物（`state/`、`exports/`、`staging/`、`config/media_policy.json`）——它们含本机绝对路径与真实会话 ID。
- 基础开源协作治理规范：Issue Templates (Bug Report / Feature Request / Config)、PR Template、CODEOWNERS、Dependabot 自动依赖更新及分支保护策略。
- **社区持久备用频道**：在 README 交流群章节与 Issue 模板 `contact_links` 中新增 Telegram 群组 `t.me/WeChatExporter`，作为微信群二维码过期后的稳定备用交流渠道。
- **开发依赖版本与 Hash 校验协同更新**：升级开发依赖套件 `flake8` 至 `7.4.1`、`pycodestyle` 至 `2.15.0`、`pyflakes` 至 `4.0.3`，并在 `requirements-dev.txt` 中重新计算并锁定 `--hash=sha256:` 完整性校验哈希；合并所有 GitHub Actions Dependabot 依赖升级 PR。

### Security
- 本层**不引入任何凭据到仓库**：MinerU token 仅通过 `systemd-creds` 加密凭据 / 0600 文件 / 环境变量在本机注入，且 `mineru_agent.py` 绝不将 token 写入 argv 或日志。
- 媒体富化运行期产物（含家服绝对路径与真实会话 ID）已加入 `.gitignore`，避免误提交。

---
## [v1.0.2] - 2026-10-13
### Fixed
- **P0 文档/客户端仓库名残留**：已统一将文档与客户端中对旧私有内部仓库名的引用指向公共仓库 `yaping-pro/wx-pad-exporter` 与 `yaping-pro/wx-tablet-mode`；客户端 User-Agent 统一为 `wx-linux-exporter-*`。
- **P0 `clients/assistant_webhook.py` 字段映射错误**：`format_payload()` 原先读取 `content` / `type`，而导出契约真实字段为 `text` / `kind` / `note` / `ts` / `sender` / `id`，导致推送空消息且 `ignore_self` 失效。已对齐 `wx-pad-exporter` 已修复版本：`text→content`、`kind→type` 并保留 `note` 回退；`process_chats()` 的 `is_sender` 判定同步修正。新增 `clients/test_assistant_webhook.py` 断言 schema 合规消息产出非空 content。
- **P0 systemd 单元 SYSTEM 作用域误用 `%h`/`%I`**：`deploy/systemd/*.service` 为非模板单元，`%h` 在系统级展开为 `/root` 致服务无法启动。改为安装期占位符 `__WXUSER__` / `__WXHOME__`，README 安装脚本同时替换两者。
- **P1 发布 tarball 为 tarbomb**：打包改为顶层前缀目录 `wx-linux-exporter-<ver>/`；README Quickstart 解压前 `mkdir -p ~/.local/share/wechat-vault`；打包步骤纳入 `CHANGELOG.md`。
- **P1 `docs/DATA_CONTRACT.md` 鉴权默认值描述错误**：手机端 `wx-pad-exporter` 实为强制 Bearer Token（默认失败关闭），已更正两端均为强制鉴权。
- **P1 CI 仅 lint 不验证发布资产**：新增 `verify-release-tarball` job，于干净临时目录解包 tarball 并执行 `tar tzf` 列表、`py_compile` 全部 `.py`、校验 `SHA-256` sidecar、运行 `scripts/selfcheck.sh`，任一失败即阻断发布。
- **P2 依赖未锁定 hash**：新增 hash 锁定的 `requirements.txt` 与 `requirements-dev.txt`（`pycryptodome==3.21.0`、`zstandard==0.23.0` 全平台 wheel `--hash=sha256:`）。
- **P2 `bin/wechat-sync.sh` 密钥工具默认值缺失回退**：`KEY_TOOL` 缺省从 `/opt/wcdb-key-tool/...` 改为优先仓库自带 `bin/extract_keys.py`，并加运行时回退。
- **P2 `CHANGELOG.md` 引用不存在的 `bin/export_service.py`**，更正为 `bin/wechat_export_api.py`。
- 文档版本戳（`docs/*.md`）统一更新至 `v1.0.2`。

---

## [v1.0.1] - 2026-10-13
### Fixed
- 明确健康检查认证策略，严格落地 fail-closed 令牌验证。
- 阐明仓库定位边界为生产家庭实验室提取之 Linux 侧消费管道与运维编排。

---

## [v1.0.0] - 2026-10-13
### Added
- 项目初次开源发布：包含 Python 数据消费服务 (`bin/wechat_export_api.py`)、架构解包、JSON Schema 校验与自动化运维编排脚本。
- CI 持续集成测试与 Release 打包脚本 (自动化生成 tar.gz 归档与 sha256 校验和)。
