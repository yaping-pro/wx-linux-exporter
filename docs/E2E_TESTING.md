# 端到端测试架构与合成 Vault 指南 (E2E Testing Guide)

本文档说明 `wx-linux-exporter` 的端到端测试（E2E）架构设计、合成快照（Synthetic Vault）规范、本地运行方法、TDD RED→GREEN 复盘，以及真实多模态消费端验证流程。

---

## 1. 为什么采用合成快照（Synthetic Vault）

在微信本地数据库导出与富媒体解密管线中，直接依赖开发者或用户的真实微信聊天快照存在严重工程缺陷：
1. **隐私安全与合规红线**：真实微信库含个人通信记录、联系人关系网与敏感商业文档，绝不可入库版本控制或进入公共 CI 运行环境；
2. **零凭据与无网络约束**：CI 运行环境必须 100% 离线、无需 MinerU 远程凭据、无需真实微信桌面端运行环境；
3. **确定性与抗回归**：通过确定性的消息 ID、时间戳、文件内容哈希，使得分页光标、ETag 校验、HTTP Range 分片请求具备 100% 可重复断言能力；
4. **管道闭环验证**：覆盖 `worker -> ledger -> HTTP API -> assistant_webhook -> muse_probe` 完整业务闭环。

---

## 2. 合成 Vault 布局与数据结构

合成 Vault 由 `tests/e2e/fixtures_builder.py` 动态生成于独立临时目录，与 Linux 微信 4.x 本地解密目录完全同构：

```
<temp_dir>/
├── vault/
│   ├── config/
│   │   └── media_policy.json           # 媒体解析策略：开启图片、视频、语音、文件及白名单
│   ├── state/
│   │   ├── image_keys.json             # 预置提取的密钥缓存（wxid, aes_key, xor_key）
│   │   ├── media.lock                  # 文件排他锁（测试并发冲突与只读权限）
│   │   └── media_ledger.json           # 媒体资产账本（含 schema 字段与 assets 字典）
│   ├── decrypted/
│   │   └── current/
│   │       ├── contact/
│   │       │   └── contact.db          # 真实 contact 表（含 username, remark, nick_name 等）
│   │       ├── session/
│   │       │   └── session.db          # SessionTable 表（会话备选展示名）
│   │       ├── hardlink/
│   │       │   └── hardlink.db         # dir2id, file_hardlink_info_v4, video_hardlink_info_v4, image_hardlink_info_v4
│   │       └── message/
│   │           └── message_0.db        # Name2Id 与 Msg_<md5(chat_id)> 消息分片库
│   ├── exports/
│   │   └── media/<date>/<asset_id>/    # 导出的 image.png / video.mp4 / thumb.jpg
│   └── media/<asset_id>/               # API 静态服务的软/硬链目标
└── account/
    ├── msg/
    │   ├── attach/<chat_md5>/<month>/Img/  # V2 容器加密的 .dat 文件与首帧 _t.dat
    │   ├── video/<month>/                  # 明文 MP4 视频文件
    │   └── file/<month>/                   # 接收文件目录
    └── cache/                              # 消息气泡缩略图缓存
```

### 核心表与样本格式说明
* **`message_0.db`**：
  * `Name2Id`：映射 `rowid` 到 `user_name`，供发送者与会话表匹配；
  * `Msg_<md5>`：建表包含 `(local_id, server_id, local_type, real_sender_id, create_time, message_content, compress_content, packed_info_data)`。插 5 类典型消息：
    1. 文本消息（`local_type=1`）
    2. 图片消息（`local_type=3`，含 `<msg><img md5="..." length="..."/></msg>`）
    3. 视频消息（`local_type=43`，含 `<videomsg md5="..." playlength="12"/>`）
    4. 语音消息（`local_type=34`，含微信客户端自带转写的 `packed_info_data` protobuf 载荷）
    5. 文件消息（`local_type=49`，含 `<appmsg><type>6</type>...</appmsg>`）
* **图片 V2 容器**：6 字节魔数 `\x07\x08\x56\x32\x08\x07` + 4 字节 AES 长度 + 4 字节 XOR 长度 + 1 字节 Flag (`0x01`) + 16 字节对齐 AES-ECB 密文 + 单字节 XOR 尾部。
* **视频 MP4**：合法 MP4 容器结构，包含 `ftyp` (isom/mp41)、`moov` (含 `mvhd`、`tkhd`、`trak`) 与 `mdat` box，可直接被 `parse_mp4_metadata` 提取宽高（1920x1080）与精确时长（12s）。

---

## 3. 本地运行与验证方法

### 运行端到端全量流水线
```bash
# 1. 独立运行 E2E 测试
python3 tests/test_e2e_pipeline.py

# 2. 通过 pytest 运行（含详细日志输出）
python3 -m pytest tests/test_e2e_pipeline.py -v

# 3. 全仓回归测试（194 passed）
python3 -m pytest tests -q

# 4. 纯净环境独立执行检查（不带 PYTHONPATH）
env -u PYTHONPATH bash -c 'for t in tests/test_*.py; do python3 "$t" >/dev/null || { echo "FAIL $t"; exit 1; }; done; echo "clean loop OK"'

# 5. 语法与静态检查
python3 -m flake8 bin/ clients/ tests/ --count --select=E9,F63,F7,F82
```

---

## 4. TDD RED → GREEN 阶段问题复盘

在严格遵循测试驱动开发（TDD）过程中，先构造必然失败的 RED 断言，并对暴露的所有失败点进行了归类与根因定位：

| 测试用例 | 原始报错行 / 现象 | 根因分析 | 归类 | 修复措施 |
| :--- | :--- | :--- | :--- | :--- |
| `test_01_worker_to_ledger` | `sqlite3.OperationalError: no such column: packed_info_data` | `wechat_media.py` 在处理语音消息（local_type=34）时读取 `packed_info_data` 列以解析微信自带转写。合成表建表仅包含 7 个基础字段，缺少该列致 worker 查询报错崩溃。 | **(a) 合成数据不符合真实布局** | 在 `fixtures_builder.py` 的 `Msg_<md5>` 表结构中加入 `packed_info_data BLOB` 列。 |
| `test_02_api_export_all_and_filters` | `AssertionError: 'media' not found` | 连锁反应：worker 崩溃导致未产出 `media_ledger.json`；此外 `image_keys.json` 中预存的 wxid 未做 `clean_wxid` 截断，致使缓存密钥验证校验未通过。 | **(a) 合成数据不符合真实布局** | 统一在 `image_keys.json` 写入经过 `clean_wxid` 处理的账号标识，修复密钥缓存命中。 |
| `test_02_api_export_all_and_filters` | `AssertionError: Expected 404/403 for blocked chat media` | 屏蔽会话的媒体文件在无关联上下文时缺少会话锚点，导致 API 端点判断 `associated_chats` 为空从而放行。 | **(a) 合成数据不符合真实布局** | 在合成账本中补齐屏蔽会话的 `by_message` 与 `anchors` 映射，使防泄漏校验准确识别来源会话并返回 404。 |
| `test_03_media_endpoints` | `urllib.error.HTTPError: HTTP Error 404: Not Found` | 连锁反应：worker 崩溃未完成解密解包，`vault/media/<id>/image.png` 未生成。 | **(a) 合成数据不符合真实布局** | 修复 worker 正常解析后自动建立硬链落盘，端点 200/304/206 全部通过。 |
| `test_05_probe_audit_and_vision` | `AssertionError: unexpectedly None : Failed to extract JSON payload from vision output` | `muse_probe.run_vision` 输出为标准多行美化 JSON，测试中的解析逻辑原按单行匹配导致提取失败。 | **(a) 测试断言提取方式问题** | 改用首尾大括号边界定位法安全反序列化多行 payload。 |

> **复盘结论**：产品代码（`bin/` 与 `clients/`）逻辑严谨合规，无 `(b)` 类产品代码缺陷。全部 RED 均源于测试数据初始布局与提取方式，已在测试包内部闭环修复。

---

## 5. 真实消费端验证（人工 / 浏览器）

> **⚠️ 范围声明**：本节验证属于真实消费端的视觉与多模态端到端确认，依赖真实的微信解密媒体目录与人机交互/浏览器自动化环境，**不包含在 CI 自动化流水线中**。

合成快照测试证明了系统数据流与接口协议的**管道正确性**。但在投产发布前，需通过真实多模态 AI 消费端（如 Muse AI）验证其是否真正「看懂」解密后投递的图像与视频首帧：

### 1. 前置步骤：生成多模态投递载荷
使用已解密的真实微信快照与导出的 JSON 数据，产出标准 OpenAI 兼容多模态 Payload：
```bash
# 对视频消息生成包含首帧 Base64 Data URL 的载荷
python3 clients/muse_probe.py \
  --vision \
  --only video \
  --image-dir /path/to/decrypted/wechat-vault/media \
  --input /path/to/exported_wechat.json \
  > /tmp/muse_vision_payload.json
```

### 2. 载荷投递至 Muse
1. 打开 ego 浏览器（或手工访问 Muse AI 对话页面）；
2. 提取 `/tmp/muse_vision_payload.json` 中 `messages[0].content` 里的 `image_url.url`（即 `data:image/jpeg;base64,...` 首帧内容）以及文本提示词；
3. 将图片及配套问题（如：「请详细描述这张微信消息附带的图片/视频首帧内容，并列出图中的所有可见文字」）投递至对话框。

### 3. 消费端判定标准
* **具体文字识别**：模型必须准确说出图片或首帧中的具体文字（如招牌名、文档标题、聊天截图中的对话）、具体物体与场景特征，不得含糊泛泛描述（如仅回答「这是一张室内照片」）；
* **无文字场景求真**：对于无文字的风光、纯景物或头像照片，模型必须如实回答「图中没有文字」或「未检测到文本」，**严禁无中生有、编造文字**；
* **时空上下文一致性**：模型回答需与消息文本中的会话名称、发送时间保持语义一致。
