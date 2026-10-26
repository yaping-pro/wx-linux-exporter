# 微信导出媒体图片 OCR 模块规范与架构文档

本文档定义 `wx-linux-exporter` 的本地 OCR 子系统设计、引擎选型、降级矩阵、隐私边界、性能基准与失败语义。

---

## 1. 架构定位与设计哲学

在 `wx-linux-exporter` 中，OCR 属于**内容层**而非**文件转换层**：
- **消费端导向**：最终消费方是个人 AI 助理（如 Dify、FastGPT、自建 LLM Agent），AI 需要理解聊天中用户发送的截图、单据、发票、通知公告等图片文字内容；
- **纯本地、零网络**：数据完全在本地（家服 Linux / 本机 macOS）处理，不向任何第三方公网 API 泄露私密聊天图片；
- **零外部强依赖**：核心导出器在没有任何 OCR 引擎安装的环境下也能正常运行，OCR 模块作为可选依赖（`requirements-ocr.txt`），缺失时优雅返回 `status="unavailable"`，绝不阻断媒体富化主链路。

---

## 2. 引擎选型与降级矩阵

### 2.1 引擎评估与选型

| 引擎 | 架构与模型 | 中文识别率 | 英文与混排 | 平均耗时 (单张) | 内存开销 | 部署依赖 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **RapidOCR** *(推荐默认)* | ONNXRuntime (CPU) / DBNet + SVTR | **优秀 (~90%+)** | 优秀 | 1.5s ~ 2.2s | ~190 MB | Python 可选包 (`rapidocr_onnxruntime`) |
| **Tesseract** *(兜底系统CLI)* | LSTM + Tesseract 5.x (`chi_sim+eng`) | 一般 (常见形近字混淆) | 良好 | 0.4s ~ 0.8s | < 50 MB | 系统包 (`tesseract-ocr`, `tesseract-ocr-chi-sim`) |
| **MinerU** *(上层专用通道)* | PDF/Docx/复杂版面端到端解析 | 顶级 | 顶级 | 10s ~ 30s | 需云端/大显存 | 位于 `bin/mineru_agent.py`，由调度层针对大文档按需调遣 |

### 2.2 降级策略

默认调用链为：`rapidocr -> tesseract -> none`。
1. **优先尝试 RapidOCR**：如果环境已安装 `rapidocr_onnxruntime`，优先使用轻量 ONNX 深度学习模型，获得极高的中英混排与屏幕截图字形识别精度。
2. **平滑降级到 Tesseract**：如果未安装 Python RapidOCR 或执行发生异常/超时，尝试调用系统已安装的 `tesseract` CLI 命令行（`-l chi_sim+eng`）。
3. **安全终态收敛**：若两者均未安装，直接返回 `status="unavailable", provider="none"`；若执行超时返回 `status="timeout"`；若图片过大/解码报错返回 `status="failed"`。

---

## 3. 部署与安装

### 3.1 方案 A：完整特性（推荐，安装 RapidOCR）

在运行 worker 的 Python 环境中安装可选依赖：
```bash
pip install -r requirements-ocr.txt
```
`requirements-ocr.txt` 包含：
- `rapidocr_onnxruntime>=1.2.3`：内含轻量 ONNX 权重（约 12MB），纯 CPU 推理，无需 CUDA 或 GPU。
- `Pillow>=10.0.0`：用于快速图片元数据读取与安全校验。

### 3.2 方案 B：轻量系统 CLI（仅安装 Tesseract）

在 Ubuntu / Debian 系统上安装 Tesseract 中文支持包：
```bash
sudo apt-get update && sudo apt-get install -y tesseract-ocr tesseract-ocr-chi-sim
```
安装体积约 45MB，无需在 Python 中引入额外第三方包。

### 3.3 方案 C：无引擎运行（最小环境）

无需安装任何包。`bin/wechat_ocr.py` 依赖仅为 Python 3.10+ 标准库，导入绝不报错，`available_providers()` 返回 `[]`，图片 OCR 自动标记为 `unavailable`。

---

## 4. 文本清洗与规整规范 (`normalize_text`)

聊天截图 OCR 输出通常包含大量噪声与断行。`normalize_text` 执行如下严格流水线：
1. **统一全半角（NFKC 归一化）**：
   - 全角数字（`１２３` → `123`）、全角字母（`ＡＢＣ` → `ABC`）规范为半角；
   - 英文标点统一，同时安全保留中文括号、书名号、破折号及省略号。
2. **孤立符号行过滤（Noise Stripping）**：
   - 自动过滤分割线（如 `---`、`======`）、孤立边框点阵（`......`、`| |`）等纯符号行，避免噪声污染文本检索。
   - 符号行同时作为天然的段落隔离符，防止上下独立区块误合并。
3. **中文/英文智能断行合并（Line Unwrapping）**：
   - **中文段内折断**：相邻行若以汉字或中文标点结尾并以汉字起始，合并为连续文本，不插入多余空格；
   - **英文连字符折断**：以连字符结尾的英文单词（如 `demon-` + `stration`）自动拼接为完整单词（`demonstration`）；
   - **词间空格保护**：英文单词与数字之间合并时保留单个空格（`WeChat` + `OCR` → `WeChat OCR`）。
4. **语义实体完备保留**：
   - 完整保留长数字、手机号、日期时间、订单号及带参数的完整 URL（如 `https://example.com/api?a=1&b=2`）。
5. **段落结构规范**：
   - 连续空行压缩为单个 `\n`，去除全文字首字尾冗余空白。

---

## 5. 隐私边界与安全防护

### 5.1 零数据泄露原则
- **禁止打印/记录敏感内容**：日志严禁输出图片二进制、识别出的具体文本内容以及敏感本地文件路径；
- **仅记录匿名统计量**：仅允许记录文件体积（字节数）、分辨率宽高、执行耗时、状态码与引擎版本。

### 5.2 内存防护与防解压炸弹
- **体积硬门禁**：单图体积超过 `30 MB` 时，直接返回 `status="failed"`，不读入解码；
- **像素硬门禁**：解析图片头部（PNG/JPEG/GIF/BMP/WEBP header），若尺寸超过 `50,000,000` 像素（5000 万像素），直接返回 `status="failed"`，杜绝内存耗尽（OOM）。

### 5.3 严格硬超时机制
- **Tesseract CLI**：`subprocess.run(..., timeout=timeout)`，超时自动 kill 进程；
- **RapidOCR In-Process**：独立后台守护线程托管，外层 `thread.join(timeout=timeout)` 硬等待，超时抛出 `TimeoutError` 并降级，避免 ONNXRuntime 底层 C++ 线程阻塞主工作进程。

---

## 6. 缓存机制 (`state/ocr_cache/`)

- **寻址方式**：内容寻址 `state/ocr_cache/<sha256>.json`；
- **缓存键**：图片的明文 `sha256` + 启用的 `providers` 列表版本串；
- **原子落盘**：先写入临时文件 `.<sha256>.tmp.<pid>`，再原子性 `os.replace`，避免并发读写半成品；
- **价值**：微信群聊中表情包、热门截图、通知公告被频繁转发到多个会话，命中缓存直接返回，避免 100% 重复算力浪费。

---

## 7. 实机测量基准与性能数据

在生产家服硬件（Ubuntu 24.04 LTS x86_64, Linux 7.0 内核）上，使用 20 张真实解密的微信 4.x 高清截图（`_h.png` / `_h.jpg`）进行实测：

### 7.1 指标统计对比

| 评估维度 | RapidOCR (CPU / ONNX) | Tesseract CLI (`chi_sim+eng`) |
| :--- | :--- | :--- |
| **测试样本总数** | 20 张真实聊天/网页截图 (19.5 KB ~ 4.98 MB) | 20 张同批样本 |
| **就绪识别率 (Ready)** | **18 / 20 (90%)** | 17 / 20 (85%) |
| **空文本判定 (Empty)** | **2 / 20 (10%)** (纯无字风景图，非失败) | 3 / 20 (15%) (漏识 1 张文字图) |
| **平均识别耗时** | 2.273 秒 | 0.793 秒 |
| **中位数耗时** | 1.552 秒 | 0.540 秒 |
| **短文本/小图耗时** | 0.713 秒 (25 KB) | 0.321 秒 |
| **长文本/大图耗时** | 8.923 秒 (98 行复杂表单) | 3.716 秒 |
| **平均置信度** | **0.82** (高可信) | 未直接提供全局置信标尺 |
| **进程常驻内存 (RSS)** | ~132 MB (模型加载) / 峰值 ~190 MB | ~30 MB (子进程瞬时释放) |

### 7.2 文本识别质量抽样比对（敏感数据已脱敏）

#### 样本 1：手机知乎截图（中文长段落）
- **RapidOCR 输出**：
  > 韩国财阀这么嚣张，为什么其他国家不帮一下？
  > 黑木崖任老爷子
  > 温酒等3227人赞同了该回答>
  > 韩国财阀的无法无天真是一次又一次刷新我的三观。
  > 最近一次，韩国某品牌的汽车。，还是豪华汽车，被发现在紧急情况下，使劲踩刹车踏板会导致刹车踏板支架断裂+。
- **Tesseract 输出**：
  > 韩国财阅这么器张，为什么其他国家不帮一下?  *(字形混淆: 财阅、器张)*
  > @ 黑木兰任老他子                         *(字形混淆: 黑木兰)*
  > 温酒 等 3227 人敌同了该回答 》             *(字形混淆: 敌同)*
  > 最近一次，韩国菜品牌的汽车                *(字形混淆: 韩国菜)*
  > 使和路着车踏板会导致沿革                  *(严重错误)*

#### 样本 2：社康医生微信聊天记录截图
- **RapidOCR 输出**：
  > 14:03
  > 新洲社康万献家(328)
  > 陈医生，早上好，麻烦你帮我家庭医生这边解约吧
- **Tesseract 输出**：
  > 14:03 ot > a)
  > 《 新洲社康万献家|1(328)
  > 陈医生，早上好，麻烦你帮有我家庭医生这边解约吧，就是我转

#### 样本 3：英文学术论文图表
- **RapidOCR 输出**：
  > Bushen Huoxue Formula Protects Cartilaginous Endplate Chondrocytes and Attenuates Endplate Degeneration Through PI3K/AKT-Associated Regulation
- **Tesseract 输出**：
  > Bushen Huoxue Formula Protects Cartilaginous Endplate Chondrocytes and Attenuates Endplate Degeneration Through PI3K/AKT-Associated Regulation

### 7.3 成本与容量测算（按 1,000 张图计算）
1. **CPU 成本**：
   - RapidOCR 纯 CPU 推理：每千张图消耗约 2,270 CPU-秒（单核心约 37.8 分钟；双并发约 19 分钟）；
   - 家服后台作为低优先级 worker（`nice -n 10`）运行，完全不影响核心微信解密与导出服务的响应。
2. **内存需求**：
   - 进程基线内存约 76MB，加载 RapidOCR ONNX 模型后常驻约 132MB，推理峰值约 190MB；
   - 建议宿主机保留至少 **256MB ~ 512MB 可用内存** 即可平稳运行。

---

## 8. 状态码与接口契约

### 8.1 结构体定义
```python
@dataclass(frozen=True)
class OcrResult:
    text: str                 # 规整后的文本，失败/无文字时为空串
    provider: str             # "rapidocr" | "tesseract" | "none"
    lang: str                 # 实际使用的语言串，如 "chi_sim+eng"
    confidence: float | None  # 0..1，取不到给 None
    status: str               # "ready" | "empty" | "unavailable" | "failed" | "timeout"
```

### 8.2 状态码表 (Status Codes)

| 状态码 (`status`) | 含义与触发条件 | `provider` | `text` | 消费端处理建议 |
| :--- | :--- | :--- | :--- | :--- |
| **`ready`** | OCR 成功识别，且提取文本长度 >= `min_chars` | `rapidocr` / `tesseract` | 归一化后的文本 | 正常索引入库，提供给 AI 检索与问答 |
| **`empty`** | 引擎执行成功，但图像无文字或文字少于阈值（如风景照） | `rapidocr` / `tesseract` | 空字符串 `""` | 终态，记为纯图像，无需重试 |
| **`unavailable`** | 请求的 OCR 引擎未在宿主机安装 | `"none"` | 空字符串 `""` | 降级终态，提示运维安装可选依赖 |
| **`failed`** | 超过体积/像素阈值、图片损坏或引擎内部崩溃 | `"none"` | 空字符串 `""` | 异常终态，记录错误统计 |
| **`timeout`** | 引擎执行超过 `timeout` 秒 | `"none"` | 空字符串 `""` | 瞬时超时，上层可根据策略稍后低频重试一次 |

---

## 9. 已知限制与处理建议

1. **极端手写体与草书**：通用 OCR 模型对非规范手写连笔字识别率有所下降，建议对医学处方等手写图保持谨慎；
2. **超低分辨率表情包**：像素低于 100x100 的缩略图可能漏识或误判为 `empty`；
3. **复杂排版双栏多栏文献**：本模块侧重单图段落规整，未接入复杂版面分析（LayoutLM）；长篇多页 PDF/复杂版面需经 `bin/mineru_agent.py` 走 MinerU 管道；
4. **竖排繁体字**：默认模型针对横排简体和中英混排优化，对于古籍等特殊排版准确率受限。
