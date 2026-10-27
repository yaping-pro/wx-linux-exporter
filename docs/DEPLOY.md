# 部署（快照式原子部署）

家服的 `wx-linux-exporter` 生产实例运行在一台 systemd 主机上，发布机制是：

> `~/.local/share/wechat-vault/bin` 是一个**软链接**，指向 `wechat-vault/.deploy/<UTC时间戳>/` 下的某个快照目录。systemd 单元 `wechat-export-api.service` 的 `ExecStart` 指向 `.../bin/wechat_export_api.py`（经软链接解析）。

因此升级 = **建新时间戳快照目录 → 原子切软链接 → 重启服务**。本仓库提供 `scripts/deploy-snapshot.sh` 固化这套流程，替代已失效且危险的旧脚本（见文末警告）。

---

## 为什么是「快照 + 原子软链接」

拷贝式部署（直接 `rsync`/`cp` 覆盖运行中的 `bin/`）必然产生**漂移**：

- 覆盖过程中若服务在跑，正在被读取的 `.py` 可能被截断，导致 500 / `py_compile` 失败。
- 增量覆盖留下了「部分新、部分旧」的中间态，出问题时分不清当前到底是哪一版。
- 没有历史快照，回滚只能靠手工 `cp` 旧备份，且往往忘了对账。

本次事故就是活例子：**旧 `deploy-vault.sh` 从 `~/.claude/skills/wechat-ops/scripts/`（一个没有媒体代码的 staging 目录）覆盖生产，导致生产倒退、与仓库漂移约 930 行 / 10 天**。快照式部署从机制上杜绝这两类问题：

- 每次发布先落到一个**全新的、只读的、完整的**快照目录，自检通过才切换。
- 切换用 `ln -sfn .deploy/<TS> .bin.tmp && mv -T .bin.tmp bin`：先建临时链接再 `mv -T` 覆盖，**`mv` 在同一目录内是原子的**，服务进程要么看到旧整体、要么看到新整体，绝不会看到半截。
- 每次快照都保留在 `.deploy/` 下，回滚就是换一个软链接目标，**零数据丢失、零手工对账**。

---

## 目录布局

```
~/.local/share/wechat-vault/
├── bin -> .deploy/20261027T183000Z          # 当前生效版本（软链接）
└── .deploy/
    ├── 20261020T120000Z/                     # 历史快照（完整 bin 内容）
    │   ├── wechat_export_api.py
    │   ├── wechat_media.py
    │   ├── ...（仅 *.py / *.sh）
    ├── 20261027T183000Z/                     # 本次快照
    ├── CHANGELOG.local                       # 每次成功的发布/回滚追加一行
    └── deploy.log                           # 每次发布的基线记录（before 状态）
```

`CHANGELOG.local` 每行格式：

```
<UTC时间> ts=<TS> commit=<short> from=<旧TS> -> <新TS> | rollback: <回滚命令>
```

---

## 用法

### 前置条件

1. **ssh 免密**：本机 `ssh <remote>` 能直接登进去（建议 `ssh-copy-id`）。
2. **sudo 免密 restart**：远端执行 `systemctl restart wechat-export-api` 无需密码（在 `/etc/sudoers.d/` 里放行该 unit）。
3. **rsync**：本机已装 `rsync`（macOS 通常需 `brew install rsync`）。
4. 远端已存在 `wechat-vault` 目录结构（首次部署见下）。

### 首次部署

```bash
# 在仓库根目录执行；--remote 默认是别名 "linux"，--vault 默认 $HOME/.local/share/wechat-vault
bash scripts/deploy-snapshot.sh --remote linux

# 先做一次干跑，确认计划步骤（不连真机、不产生任何副作用）
bash scripts/deploy-snapshot.sh --remote linux --dry-run
```

首次部署时 `bin` 还不存在，脚本会 `mkdir` 快照目录、rsync、自检、建立 `bin -> .deploy/<TS>` 软链接、重启并断言健康检查。

### 日常升级

```bash
# 改完代码后，从仓库根目录发布（脚本默认 --source 就是 <repo>/bin）
bash scripts/deploy-snapshot.sh --remote linux

# 只发布部分来源目录（如换一台机的 bin 位置）
bash scripts/deploy-snapshot.sh --remote linux --source ./bin
```

流程顺序（任一步失败自动回滚到切换前的快照）：

1. 记录基线：`readlink -f bin`、服务 `is-active`、`/health` 状态码 → `deploy.log`。
2. 取 UTC 时间戳 `TS`，远端 `mkdir -p .deploy/$TS`。
3. `rsync -az`（仅 `*.py`/`*.sh`，排除 `__pycache__/`、`*.pyc`、`*.egg-info/`）。
4. 远端自检：`python3 -m py_compile *.py` + `wechat_export_api.py --help`（超时 20s）。
5. 原子切换：`ln -sfn .deploy/$TS .bin.tmp && mv -T .bin.tmp bin`。
6. `systemctl restart`，`sleep 2`，`is-active` 必须 `active`。
7. 健康断言：`curl -sf http://127.0.0.1:8765/health`（重试 5 次 × 1s）。
8. 成功则向 `CHANGELOG.local` 追加一行（含回滚命令）。

### 回滚

```bash
# 切回指定快照并重启 + 健康断言（不做 rsync）
bash scripts/deploy-snapshot.sh --rollback 20261020T120000Z --remote linux

# 回滚命令本身就在 CHANGELOG.local 里，直接复制即可
```

`--rollback <TS>` 只做切换 + 重启 + 健康检查，**不会 rsync、不会触碰 `.deploy/` 内容**，因此安全可重复。

---

## 安全检查清单

**切前：**
- [ ] 已 `git pull` 到想发布的 commit，且本地 `bin/` 是该 commit 的内容。
- [ ] 已用 `--dry-run` 跑过一遍，确认计划步骤无误。
- [ ] 远端旧 `bin` 软链接目标已记下（脚本会自动写进 `deploy.log` 与 `CHANGELOG.local`）。

**切后四项验证（脚本自动完成，但人工复核时同样适用）：**
- [ ] 软链接 `readlink -f ~/.local/share/wechat-vault/bin` 指向新 `TS`。
- [ ] `systemctl is-active wechat-export-api` == `active`。
- [ ] `curl -sf http://127.0.0.1:8765/health` 返回 2xx。
- [ ] `cat .deploy/CHANGELOG.local` 出现本次发布的那一行（含回滚命令）。

**回滚命令（随时可用）：**
```bash
bash scripts/deploy-snapshot.sh --rollback <旧TS> --remote linux
```

---

## ⚠️ 危险警告

> **不要运行旧的 `~/.claude/skills/wechat-ops/deploy/deploy-vault.sh`。**

该脚本从 `~/.claude/skills/wechat-ops/scripts/`（一个**没有媒体代码**的 staging 目录）直接 `rsync` 覆盖生产 `bin/`，运行后会把生产**倒退到缺代码版本**。它已失效且与本仓库机制不符。本仓库的 `scripts/deploy-snapshot.sh` 是唯一受支持的发布入口，默认 `--source` 就是仓库自身的 `bin/`，保证发布的就是当前 commit 的代码。

---

## 与 GHCR 镜像方案的关系

当前**不采用** GHCR 容器镜像（`docker pull ghcr.io/...`）作为家服发布路径，原因有三：

1. **systemd `LoadCredentialEncrypted`**：家服单元用 `systemd-creds` 加密注入 token，容器方案需要把凭据重新落盘或以别的方式注入，破坏了既有的零落盘凭据模型。
2. **跨卷 hardlink `EXDEV`**：`--volume` 挂载的 vault 与镜像层跨文件系统，OverlayFS / 容器内 `mv -T` 或 hardlink 会撞 `EXDEV`（Invalid cross-device link），快照式软链接切换的原子性保障在容器挂载语义下变脆。
3. **Tailscale 网卡**：家服仅通过 Tailscale 暴露管理面，容器网络的 `127.0.0.1:8765` 与 host 网络命名空间隔离，健康检查 / 重启语义都要额外 bridge，得不偿失。

因此家服继续走「裸机 systemd + 快照软链接」方案；GHCR 镜像仅用于 CI 验证与 Docker/Compose 隔离部署（见 [DOCKER.md](DOCKER.md)），两者职责分离。
