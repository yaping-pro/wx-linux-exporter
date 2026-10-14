# 变更日志 (Changelog)

本项目所有显著变更均记录于此文档。
本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/) 语义化版本规范，格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

---

## [Unreleased]
### Added
- 基础开源协作治理规范：Issue Templates (Bug Report / Feature Request / Config)、PR Template、CODEOWNERS、Dependabot 自动依赖更新及分支保护策略。
- **社区持久备用频道**：在 README 交流群章节与 Issue 模板 `contact_links` 中新增 Telegram 群组 `t.me/WeChatExporter`，作为微信群二维码过期后的稳定备用交流渠道。

### Changed
- **开发依赖版本与 Hash 校验协同更新**：升级开发依赖套件 `flake8` 至 `7.4.1`、`pycodestyle` 至 `2.15.0`、`pyflakes` 至 `4.0.3`，并在 `requirements-dev.txt` 中重新计算并锁定 `--hash=sha256:` 完整性校验哈希；合并所有 GitHub Actions Dependabot 依赖升级 PR。
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
