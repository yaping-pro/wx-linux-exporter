## 变更概述 / Pull Request Overview

<!-- 请简要说明本 PR 包含的改动与背景 / Briefly explain what this PR does and why -->

### 关联 Issue / Related Issues
<!-- 如有请关联 / Link issues -->
Closes #

---

## 变更范围 / Scope of Change

- [ ] `bin/`: 消费服务与导出核心 (`export_service.py` 等)
- [ ] `clients/`: 客户端适配与下游消费桥接
- [ ] `ops/` / `scripts/`: Linux 部署守护与运维脚本
- [ ] `schema/`: JSON 数据规范
- [ ] `docs/`: 文档与架构说明

> ⚠️ **边界提醒**：Android 手机端 Hook 探针与守护进程源码位于独立上游仓库。本仓库不接收设备端源码相关修改。

---

## 测试与验证 / How Has This Been Tested?

请描述验证步骤与执行的测试：
- [ ] Python 语法编译检查通过: `python3 -m py_compile bin/*.py clients/*.py`
- [ ] 代码静态检查通过: `flake8 bin/ clients/ --count --select=E9,F63,F7,F82`
- [ ] JSON Schema 语法通过: `python3 -c "import json; json.load(open('schema/export.schema.json'))"`
- [ ] 运维 Shell 语法检查通过: `bash -n bin/*.sh scripts/*.sh`
- [ ] 本地自检脚本通过: `bash scripts/selfcheck.sh` *(若适用)*

---

## 贡献合规自查清单 / Contributor Checklist

- [ ] **零敏感数据 (Zero PII / Zero Secrets)**: 严禁包含真实微信聊天文本、会话记录、解密密钥、私网真实 IP 或真实 Token。
- [ ] **遵循代码风格**: 遵循 Conventional Commits 提交信息规范。
- [ ] **开源许可授权 (MIT)**: 我确认贡献代码完全合规，并同意依本仓库 MIT 许可证开源。
