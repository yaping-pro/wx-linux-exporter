# TROUBLESHOOTING (`TROUBLESHOOTING.md`)

[中文](#中文) | [English](#english)

---

## 中文

### 1. 微信进程启动后退出 / 崩溃
- **现象**：`systemctl status wechat-runner.service` 显示退出代码非 0。
- **排查**：
  1. 确认 `Xvfb` 是否已正常运行在 `:99`：`ps aux | grep Xvfb`。
  2. 尝试手动调试运行：`DISPLAY=:99 /opt/wechat/wechat` 查看控制台输出。
  3. 确认是否缺少桌面环境依赖：`sudo apt-get install -y libxss1 libappindicator3-1 libsecret-1-0`。

### 2. 密钥提取失败
- **现象**：`wechat-sync.sh` 日志输出 `GDB attach timeout` 或 `Key not found`。
- **排查**：
  1. 确认系统 ptrace 权限：检查 `/proc/sys/kernel/yama/ptrace_scope`，若为 1 或更高，可执行 `sudo sysctl kernel.yama.ptrace_scope=0` 临时放行。
  2. 确认微信是否处于已登录状态。若微信刚重启，尚未完成用户登录，则内存中尚未初始化加密密钥。

### 3. 解密 WAL 损坏或报错
- **现象**：`WalFormatError: magic mismatch`。
- **排查**：
  - 微信正在高频写入脏页。`wechat_publish.py` 会自动捕获异常并保留上一稳定版本（`current` 不切换），在下次定时周期自动重试。无需手动干预。

### 4. 远程无法连接导出端口 8765
- **现象**：`curl` 连接超时或拒绝。
- **排查**：
  - 默认配置仅监听 `127.0.0.1`。若跨机拉取，请在 `export_service.json` 中配置为本机的私网地址（如 Tailscale IP `100.x.y.z`），或通过 SSH 隧道转发：
    ```bash
    ssh -L 8765:127.0.0.1:8765 user@homelab-host
    ```

### 5. 媒体富化：文件解析不出来 / 语音没有文本

先看账本里的 `deny_reason`（这是唯一权威判据，不要靠猜）：

```bash
python3 -c "import json;d=json.load(open('state/media_ledger.json'));\
from collections import Counter;print(Counter(v.get('deny_reason') for v in d['assets'].values()))"
```

| 现象 | 原因 | 处置 |
| :--- | :--- | :--- |
| 大量 `agent_unsupported` | 该格式 agent 通道不支持（html/doc/xls/ppt） | 配置 precise 凭据 |
| 大量 `missing_source` | 消息有记录但磁盘无实体——微信**点击才下载**，headless 客户端不会自动落盘所有附件 | 属正常现象；可在客户端点开该文件后再跑一轮 |
| `oversize` / `page_limit` | 超出通道上限 | 配 precise，或手动 `--pages` |
| 语音全部 `voice_not_transcribed` | 客户端未开启自动转写，或语音在开启之前收到 | 开启 `Auto-convert voice messages to text`；历史语音需在客户端手动点 "Convert to text" |
| `no_credential` | 路由到了 precise 但没有 token | 见 [`MEDIA_ENRICHMENT.md`](MEDIA_ENRICHMENT.md) §6 |
| 日志无 `precise_token=available` | 凭据未挂载 | 检查 `LoadCredentialEncrypted=` 是否取消注释并 `daemon-reload` |

其它要点：
- **`msg/file` 是活动目录**：worker 会先复制到 `staging/` 并校验大小稳定；若某文件持续失败，多半是微信仍在写入。
- **`hardlink.db` 表结构随微信版本变化**：升级微信后如出现大面积 `missing_source`，先核对 `file_hardlink_info_v4` 是否仍存在。
- **确认 worker 真的在跑**：`systemctl list-timers wechat-media.timer`；策略 `enabled=false` 时它每轮都是 no-op（这是设计行为）。

### 6. 索引静默冻结：导出 API 一直 200，但今天一条新消息都没有

生产实例 2026-11-08 22:45 → 11-09 22:22 完整踩过一次，**两个独立故障叠加**，必须分别排查：

**(a) 微信客户端掉到登录页（数据源死了）**

```bash
# 源库最后写入时间：停了多久
find ~/Documents/xwechat_files/*/db_storage -name "*.db" -newermt "-2 hours" | head
# 客户端窗口尺寸：292x396 就是扫码登录面板；正常主窗口 >900px 宽
DISPLAY=:99 xwininfo -root -tree | grep Weixin
# 当天日志量：正常 80–100MB/天，登录页空转只有几百 KB
ls -la ~/.xwechat/log/mm_$(date +%Y%m%d).xlog
```

客户端重启（含主机重启）后**不会**自动恢复会话：登录页停在原地等人。登录页（`292x396`）的
二维码区域在**没有残留设备会话时是一个静止的灰度骨架占位**（像素均值 ≈74、无模块纹理），
此时点击它不会刷新，必须扫码；扫码需要一块人眼可见的屏（Xvfb `:99` 是不可见的）。

**已有设备会话时不必扫码**——登录页中下部有一颗宽按钮（窗口内相对坐标 `+145,+306`，
即屏幕 `+145,+306` 处，窗口原点由 `xwininfo` 给出），点击即"进入微信"，客户端会自动恢复：

```bash
DISPLAY=:99 xwininfo -root -tree | grep Weixin      # 取 +X+Y
DISPLAY=:99 xdotool mousemove $((X+145)) $((Y+306)) click 1
DISPLAY=:99 xdotool mousemove 20 780                # 把指针移开，避免污染截图
DISPLAY=:99 xwininfo -root -tree | grep Weixin      # 成功后窗口变为 ~917x668 主界面
find ~/Documents/xwechat_files -name "*.db-wal" -newermt "-3 minutes" | head   # WAL 重新写入
```

2026-11-09 实测：点击后 30 秒内 `message_0.db-wal` 恢复写入、`session.db` 最新 `last_timestamp`
追到当下，`wechat-decrypt.timer` 下一轮发布即带回当天全部消息。这是家服无人值守的最大单点：
**主机重启后必须有人点一下登录页**（或扫码），否则数据源永久静默。

**(b) timer 重启后永久静默（发布链路死了）**

```bash
systemctl list-timers wechat-decrypt.timer --all --no-pager
systemctl show -a wechat-decrypt.timer | grep -E "TimersCalendar|TimersMonotonic|NextElapse"
```

若 `NEXT` 为 `-`、`NextElapseUSecMonotonic=infinity`：触发项丢了。根因是
`OnUnitActiveSec=` / `OnCalendar=` 这类**空赋值会重置整个计时器列表**（连同
`OnCalendar` 与 `OnBootSec`）——重启后没有激活基准，`next_elapse=0`，永不触发。
修复：删掉带空赋值的 drop-in（`/etc/systemd/system/wechat-decrypt.timer.d/interval.conf`），
只保留 `OnCalendar`/`OnBootSec`，`daemon-reload` 后 `restart`，确认 `NEXT` 有值。
`tests/test_systemd_units.py` 已把这条固化为静态守卫。

> **水位会撒谎**：数据契约规定「无新消息时 watermark 退化为 `range.to`」（`docs/DATA_CONTRACT.md` 增量语义）。
> 所以数据源死掉时 API 依然返回 200 + 新鲜水位 + 0 条消息。**判断数据是否新鲜，必须看 `manifest.message_count`
> 与库里最大 `ts`，不能只看 watermark。**

---

## English

### 1. WeChat Process Crashes
- Verify `Xvfb :99` is running.
- Run `DISPLAY=:99 /opt/wechat/wechat` manually to inspect console error logs.

### 2. Key Extraction Fails
- Check ptrace permissions via `sudo sysctl kernel.yama.ptrace_scope=0`.
- Verify WeChat is logged in (keys do not exist before login).

### 3. Port 8765 Inaccessible Remotely
- Default binding is `127.0.0.1`. Use Tailscale or SSH tunneling (`ssh -L 8765:127.0.0.1:8765`) to reach it securely.
