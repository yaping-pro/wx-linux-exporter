# 变更日志 (Changelog)

本项目所有显著变更均记录于此文档。
本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/) 语义化版本规范，格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

---

## [Unreleased]
### Added
- （待填）

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
