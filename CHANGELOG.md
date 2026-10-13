# 变更日志 (Changelog)

本项目所有显著变更均记录于此文档。
本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/) 语义化版本规范，格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

---

## [Unreleased]
### Added
- 基础开源协作治理规范：Issue Templates (Bug Report / Feature Request / Config)、PR Template、CODEOWNERS、Dependabot 自动依赖更新及分支保护策略。

---

## [v1.0.1] - 2026-10-13
### Fixed
- 明确健康检查认证策略，严格落地 fail-closed 令牌验证。
- 阐明仓库定位边界为生产家庭实验室提取之 Linux 侧消费管道与运维编排。

---

## [v1.0.0] - 2026-10-13
### Added
- 项目初次开源发布：包含 Python 数据消费服务 (`bin/export_service.py`)、架构解包、JSON Schema 校验与自动化运维编排脚本。
- CI 持续集成测试与 Release 打包脚本 (自动化生成 tar.gz 归档与 sha256 校验和)。
