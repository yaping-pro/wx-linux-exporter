# 免责声明 / Disclaimer

[中文](#中文) | [English](#english)

---

## 中文

### 1. 软件性质与使用目的
本项目（`wx-linux-exporter`）为**个人数据备份与本地自动化集成工具**，旨在帮助用户备份自身合法持有的 Linux 微信本地数据库文件，并以只读方式暴露标准 JSON 快照，用于下游个人知识库、AI 助理及归档需求。

### 2. 合规与平台条款 (ToS) 声明
- 本项目不包含任何腾讯官方二进制或私有代码。
- 密钥提取与数据库读取依赖开源逆向分析工具链（如 `wcdb-key-tool`）。读取和解密本地存储的数据属于个人计算机上的本地数据处理，但请注意：
  - 在服务器/无头环境常驻运行微信可能涉及违反《腾讯微信软件许可及服务协议》中关于在未经许可的设备或环境下使用软件的条款。
  - 使用者须自行承担因常驻或多端登录可能引起的账号风控、限制或封禁风险。
- 本项目仅支持**只读导出**，严禁用于任何形式的消息群发、自动化营销、机器人骚扰等侵犯第三方合法权益的行为。

### 3. 免责条款
- 本项目按“现状”（AS IS）提供，不提供任何明示或暗示的担保。
- 作者与贡献者对使用本项目所导致的任何数据丢失、账号受限、硬件损坏或法律责任概不负责。

---

## English

### 1. Software Nature & Purpose
`wx-linux-exporter` is an open-source **personal data backup and local automation pipeline**. It is designed to help users archive and decrypt their own local WeChat database files on Linux, serving standardized read-only JSON snapshots for downstream personal assistants and knowledge vaults.

### 2. Compliance & Terms of Service (ToS)
- This project contains zero proprietary binaries or Tencent source code.
- Key extraction and database decryption rely on open-source reverse-engineering toolchains (e.g., `wcdb-key-tool`). While inspecting and managing files stored on your own physical hardware is local data processing:
  - Running WeChat headless in a background daemon may contravene the Tencent WeChat Software License and Service Agreement.
  - Users assume all risks regarding platform risk control, account limitation, or suspension.
- This software is strictly **read-only** and must never be used for automated marketing, spam, or infringing upon third-party rights.

### 3. Limitation of Liability
- THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.
- Under no circumstances shall the authors or copyright holders be liable for any claim, damages, account suspension, or data loss arising from the use of this software.
