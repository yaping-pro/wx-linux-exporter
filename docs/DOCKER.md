# Docker 部署与家庭服务器编排指南

本文档介绍如何在家庭服务器（Homelab / Linux 主机）上通过 Docker 与 Docker Compose 部署 `wx-linux-exporter`。

---

## 核心安全设计约束

1. **只读挂载 (`:ro`)**：容器对微信解密数据库及媒体目录采用严格只读访问，保证宿主机数据绝对安全，绝无覆写损坏风险。
2. **非 Root 运行**：镜像默认采用 UID 1000（`USER 1000:1000`），可通过环境变量 `PUID`/`PGID` 自定义映射。配合 `read_only: true` 只读根文件系统与内存级 `tmpfs: /tmp`。
3. **密钥零落盘**：鉴权 Token 严格通过环境变量 `WECHAT_TOKEN` 显式注入；Docker Compose 配置使用 `${WECHAT_TOKEN:?}` 强校验，未配置拒绝启动。
4. **监听回环绑定**：容器内部监听 `0.0.0.0:8765`，宿主机端口严格绑定为 `127.0.0.1:8765:8765`，防止暴露到局域网或公网。
5. **极简轻量镜像**：基于 `python:3.12-slim` 多阶段构建，仅安装必需运行时依赖（`pycryptodome`、`zstandard`）；OCR（RapidOCR + ONNXRuntime）支持按需构建。
6. **无外部定时依赖**：媒体富化 Worker 使用轻量 shell 循环（`sleep 300`），无需引入 cron 或复杂守护进程。

---

## ⚡ 三步极速跑起来

### 步骤 1：准备 Token 与环境变量

在运行目录下生成安全 Token 并写入环境变量（或保存为 `.env` 文件，注意权限）：

```bash
# 生成 32 字节高强度随机 Token
export WECHAT_TOKEN=$(openssl rand -hex 32)
echo "生成的 Token: ${WECHAT_TOKEN}"

# （可选）若微信数据不在 ~/.local/share/wechat-vault，指定宿主机路径：
export WECHAT_VAULT_HOST_DIR=~/.local/share/wechat-vault
```

### 步骤 2：启动 API 导出服务

```bash
# 仅启动导出 API 服务（api profile，自动从 GHCR 拉取预构建镜像）
docker compose --profile api up -d
```

### 步骤 3：验证运行状态

```bash
# 1. 验证健康检查端点（无需鉴权）
curl -s http://127.0.0.1:8765/health
# 预期输出: {"status":"ok"}

# 2. 验证加密导出接口（需 Bearer 鉴权）
curl -s -H "Authorization: Bearer ${WECHAT_TOKEN}" \
  "http://127.0.0.1:8765/api/wechat/export?limit=5" | head -n 20
```

---

## 镜像来源与更新 (GHCR)

项目通过 GitHub Container Registry (GHCR) 提供官方自动化构建的多架构镜像：

```
ghcr.io/yaping-pro/wx-linux-exporter:latest
```

### 1. 多架构支持
每批次镜像均由 CI 基于原生/QEMU 环境交叉编译，同时发布并验证两种硬件架构：
- `linux/amd64`：标准 x86_64 服务器与 PC
- `linux/arm64`：ARM64 设备（如树莓派 4/5、Apple Silicon Docker 主机、甲骨文 ARM 实例等）

直接执行 `docker pull` 会由 Docker 根据宿主机内核自动拉取对应架构的分层。

### 2. 手动拉取镜像
```bash
docker pull ghcr.io/yaping-pro/wx-linux-exporter:latest
```

### 3. 一键拉取与平滑升级路径
在 `docker-compose.yml` 所在目录直接执行：
```bash
# 1. 拉取 GHCR 远端最新镜像
docker compose pull

# 2. 重启服务（若镜像有更新会自动重建容器，数据卷及挂载不受影响）
docker compose --profile api up -d
```

### 4. 版本锁定与固定版本运行
`docker-compose.yml` 采用环境变量插值 `${WXLE_IMAGE:-ghcr.io/yaping-pro/wx-linux-exporter:latest}`。若生产环境需要固定特定发布版本（如 `1.4.0`）：

```bash
# 方式 A：临时环境变量指定
WXLE_IMAGE=ghcr.io/yaping-pro/wx-linux-exporter:1.4.0 docker compose --profile api up -d

# 方式 B：在当前目录 .env 中持久化
echo "WXLE_IMAGE=ghcr.io/yaping-pro/wx-linux-exporter:1.4.0" >> .env
docker compose --profile api up -d
```

### 5. 本地构建回退 (Local Build)
若你需要定制源代码或自行构建镜像，有两种方式：
1. **解注释构建块**：编辑 `docker-compose.yml`，解开 `api`/`worker` 下的 `build:` 块注释，然后执行 `docker compose --profile api up -d --build`；
2. **指定本地镜像名**：先执行 `docker build -t wx-linux-exporter:local .`，然后通过 `WXLE_IMAGE=wx-linux-exporter:local docker compose --profile api up -d` 启动。


## 服务编排 Profile

`docker-compose.yml` 提供了两种 Profile，可按需启动：

| Profile | 服务名称 | 说明 | 启动命令 |
| :--- | :--- | :--- | :--- |
| `api` | `wx-exporter-api` | 提供 HTTP 导出与检索接口 | `docker compose --profile api up -d` |
| `worker` | `wx-exporter-worker` | 周期性执行媒体扫描与富化解析（每 5 分钟） | `docker compose --profile worker up -d` |

同时启动 API 与 Worker：
```bash
docker compose --profile api --profile worker up -d
```

---

## PUID / PGID 权限定制

如果宿主机微信数据目录属于非 1000 的特定用户，可以通过环境变量传递当前用户的 UID 与 GID：

```bash
PUID=$(id -u) PGID=$(id -g) WECHAT_TOKEN="<你的Token>" docker compose --profile api up -d
```

---

## OCR 可选构建

默认构建不包含体积较大的 ONNX 运行时与 OCR 模型。若需开启本地离线 OCR 富化功能：

```bash
# 构建带 OCR 支持的镜像
docker build --build-arg INSTALL_OCR=1 -t wx-linux-exporter:ocr .

# 或在 compose 启动时覆盖构建参数
INSTALL_OCR=1 docker compose --profile api up -d --build
```

---

## 常见故障排查 (Troubleshooting)

### 1. `WECHAT_TOKEN: WECHAT_TOKEN environment variable must be explicitly set`
- **原因**：Compose 启用了强制变量校验，防止无 Token 导致接口裸奔或启动失败。
- **解决**：在终端执行 `export WECHAT_TOKEN="<your_token>"`，或在同级目录下建立 `.env` 文件。

### 2. 挂载目录权限问题 (`Permission denied`)
- **原因**：宿主机的解密目录对容器内用户不可读。
- **解决**：
  1. 检查宿主机目录权限：`ls -ld ~/.local/share/wechat-vault/decrypted/current`
  2. 启动时传入匹配宿主机目录属主的 `PUID`/`PGID`：
     ```bash
     PUID=$(id -u) PGID=$(id -g) docker compose --profile api up -d
     ```

### 3. 数据为空或提示路径不存在
- **原因**：宿主机路径映射未对齐。
- **解决**：默认挂载 `${WECHAT_VAULT_HOST_DIR:-~/.local/share/wechat-vault}` 到容器内的 `/vault:ro`。确保宿主机目录结构包含 `decrypted/current/`，或显式设置 `WECHAT_VAULT_HOST_DIR` 环境变量指向真实路径。
