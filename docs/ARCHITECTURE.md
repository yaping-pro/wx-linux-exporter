# ARCHITECTURE (`ARCHITECTURE.md`)

[中文](#中文) | [English](#english)

---

## 中文

### 核心设计哲学：只读解密与原子版本发布

微信在运行过程中频繁向 WCDB 写入临时 WAL 帧（Write-Ahead Log）。直接读取运行中数据库会导致数据锁冲突或读取脏数据。本项目采用**写隔离与版本化原子发布**设计：

```
[原生 Linux 微信] ──(内存密钥提取)──> wcdb-key-tool ──> keys/all_keys.json
       │ (磁盘只读拷贝)
       ▼
[原始 .db 副本] ──(AES-CBC-256 解密)──> [新版本目录 decrypted/versions/<TS>-<PID>/]
                                                        │
                                         (wechat_wal 二进制合并)
                                                        │
                                                        ▼
                                              [完整合并的 SQLite 数据库]
                                                        │
                                            (atomic_symlink 原子切换)
                                                        │
                                                        ▼
                                            [decrypted/current 软链接]
                                                        ▲
                                                        │ (只读查询)
                                            [wechat_export_api.py (8765)]
                                                        ▲
                                                        │ (Bearer Token)
                                            [下游消费: Muse / Webhook / Agent]
```

### 1. 密钥提取与缓存
利用 `wcdb-key-tool` 通过 GDB 附加或 ELF 符号偏移扫描提取 SQLCipher4 密钥与 raw key。密钥缓存至 `keys/all_keys.json` (权限 0600)。

### 2. WAL 帧合并 (`wechat_wal.py`)
WCDB 活跃写入均保存在 `-wal` 文件中。解密后，工具通过解析 WAL 头帧与校验和，直接将脏页写回主 SQLite 数据库，确保离线导出的消息最新且无事务残留。

### 3. 原子切换与版本回滚
每次同步生成唯一的 `<UTC_TS>-<PID>` 目录。解密并验证完整性后，通过 `os.replace` 原子替换 `current` 软链接。上一版本保留在 `previous`，任何异常均可通过 `bin/vault-rollback.sh` 毫秒级回滚。

---

## English

### Core Philosophy: Read-Only Decryption & Atomic Version Swapping

Directly querying active WeChat databases causes locks and inconsistencies. This pipeline uses **write isolation and atomic versioned publishing**:

1. **Key Extraction**: Safe memory extraction via GDB/ELF analysis, cached at `keys/all_keys.json`.
2. **In-place WAL Merging**: Decrypted pages and WAL frame logs are merged into a clean standalone SQLite database.
3. **Atomic Symlink Switching**: Completed snapshots land in unique versioned directories `<UTC_TS>-<PID>`. The `current` symlink is atomically swapped via `os.replace`.
4. **Instant Rollback**: If an issue occurs, `bin/vault-rollback.sh` flips back to `previous` in milliseconds.
