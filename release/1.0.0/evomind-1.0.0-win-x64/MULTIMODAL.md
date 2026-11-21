# 多模态能力评估与改造

> 评估对象：EvoMind（`I:\workbuddyAI\2026-10-20-19-56-12\evomind`）
> 评估时间：2026-10-23
> 结论：**改造前不支持多模态**。已补全图片 / 音频 / 视频 / PDF 四条通道，文字以外的网络内容现在可以检索、解析、提炼、入库、问答。

---

## 一、结论：改造前只有一条文本通道

一句话：**它只认 `text/html`。** 图片、音频、视频、PDF 在抓取阶段就被判为「非文本内容」直接丢弃，检索阶段还会主动把这类 URL 过滤掉。

| 内容类型 | 改造前 | 具体表现 |
|---|---|---|
| 网页文字 | ✅ 支持 | trafilatura + lxml 提取正文 |
| PDF | ❌ 不支持 | 扩展名在候选过滤里被剔除；`fetcher` 遇到 `application/pdf` 报「非文本内容」 |
| 图片（网络） | ❌ 不支持 | 同上；视觉模型只服务于「本地扫描件书库」的 OCR |
| 音频 | ❌ 完全缺失 | 无 ASR、无解码、无字幕通道 |
| 视频 | ❌ 完全缺失 | 无平台解析、无字幕、无抽帧 |
| 图文问答 | ❌ 不支持 | `/api/chat` 只接受字符串问题，没有图片入参 |

### 代码级证据（改造前的行为）

1. **抓取层硬拒非文本** — `backend/fetcher.py` 通用分支：
   ```python
   if "html" not in ctype and "xml" not in ctype and "text" not in ctype:
       doc["error"] = f"非文本内容: {ctype}"   # 图片/音频/视频/PDF 到此为止
       return doc
   ```
2. **检索层主动剔除媒体** — `backend/evolve.py` 的 `_pick_targets()`：
   ```python
   if any(x in url for x in (".pdf", ".zip", ".png", ".jpg", ".jpeg", ...)):
       continue        # 连 PDF 都不读，更别说音视频
   ```
3. **推理层没有多模态字段** — `backend/llm.py` 的 `chat()` 只拼 `content` 字符串，从未使用 Ollama 原生的 `messages[].images`。
4. **唯一的多模态能力被限制在本地书库** — `backend/ocr.py` 的 `ocr_vision()` 只能处理「PDF 渲染出来的页面」，且只能由书库 OCR 任务调用；它还把配置键写成了 `ollama_host`（实际键名是 `ollama_url`），一旦用户改过端口就静默失败。
5. **检索源全是文本** — `search.py` 的 `DEFAULT_SOURCES` 是 docs/brave/bing/web/se/arxiv/github/wiki，没有任何图片、音频、视频源。
6. **知识条目无模态信息** — `knowledge` 表的 `source_kind` 只会写 `web` / `repo`，无法区分来源是图片还是视频。

---

## 二、改造后的架构：四条通道，一个出口

所有模态最终都变成**文本 + 模态标签**，然后走原有的「提炼 → 证据核对 → 去重入库」管线。这样做的原因：知识层（RAG）、权重层（LoRA）、问答链路完全不用改，多模态只是把「怎么读」补齐。

```
                ┌────────────── 检索 ──────────────┐
                │ 文字源（原有）                    │
                │ + Commons / Openverse（图片）     │
                │ + Internet Archive（音视频）      │
                │ + YouTube via yt-dlp（视频）      │
                └───────────────┬──────────────────┘
                                ↓
   ┌──────────────────── 解析（backend/media.py）────────────────────┐
   │ 图片 → 视觉模型（描述+转录文字）→ 失败退 RapidOCR                 │
   │ 音频 → faster-whisper 转写（带时间戳）                            │
   │ 视频 → 平台字幕优先（vtt/json3/ttml）→ 无字幕转写音轨 → 可选抽帧  │
   │ PDF  → PyMuPDF 文本层 → 无文本层（扫描件）逐页 OCR                │
   └───────────────────────────┬─────────────────────────────────────┘
                               ↓  统一 Doc{ok,title,text,kind,meta}
              提炼（按模态选提示词）→ 逐字证据核对 → 去重入库
                               ↓
              knowledge.source_kind = web / pdf / image / audio / video
```

---

## 三、关键改动点

### 1. 新增 `backend/media.py`（约 700 行，核心）

| 能力 | 实现要点 |
|---|---|
| 类型识别 `kind_of()` | 平台页 > MIME > 扩展名。平台页必须先判——`youtube.com/watch` 的 Content-Type 也是 `text/html` |
| 无扩展名探测 `probe()` | HEAD 拿 Content-Type，服务器不允许 HEAD 时退 `Range: bytes=0-0`。CDN 链接 `/a1b2c3` 靠这个才认得出来 |
| 下载 `download()` | 流式写入 `data/media`，**超过 `media_max_mb` 立即中止**，不把磁盘写满；支持 `file://` |
| 图片 | 视觉模型（`vision_model`）+ 结构化提示词（【画面】/【文字】/【要点】）；失败自动退 RapidOCR；送模型前压到 1600px |
| 音频 | faster-whisper，`vad_filter` 去静音、`beam_size=1`（只做知识抽取）、超过 `asr_max_seconds` 停止解码 |
| 视频 | 字幕优先（最省算力）：人工字幕 > 自动字幕，vtt > json3 > ttml；无字幕才 yt-dlp 抓音轨转写；本地文件可抽帧给视觉模型 |
| PDF | 文本层 < 200 字判定为扫描件，逐页渲染 OCR（最多 12 页） |
| 能力探测 `capabilities()` | 逐项报告 vision/asr/subtitles/pdf/ocr/frames 是否可用 + 缺什么，缓存 8 秒 |

**降级原则**：`extract()` 永不抛异常。缺组件时返回 `ok=False` + 中文原因（「未安装 yt-dlp，无法解析 YouTube…」），学习循环不会因为一个坏链接中断。

### 2. 抓取层分派（`backend/fetcher.py`）

`fetch()` 入口先判类型，媒体直接转 `media.extract()`；无扩展名地址在响应头里发现是媒体时，二次分派（已消费一次 GET，比丢内容划算）。

### 3. 检索层新增媒体源（`backend/search.py`）

| 源 | 覆盖 | 是否要 Key |
|---|---|---|
| Wikimedia Commons | 图片/音频/视频，CC 授权，带结构化元数据 | 免 |
| Openverse | 图片/音频（CC Search 后继） | 免 |
| Internet Archive | 讲座录音、会议录像、老教材 | 免 |
| YouTube（yt-dlp） | 平台视频 | 免 |

`search_media()` 并发多源、按 URL 去重、按模态对齐源能力（Openverse 无视频、Archive 无图片、YouTube 只有视频）。Internet Archive 的查询改成逐词 `AND`——默认的宽松 OR 会让「3d printing tutorial」返回一堆 Art of Illusion 教程。

### 4. 学习管线（`backend/evolve.py`）

- `_pick_targets()`：不再无条件剔除 `.pdf/.png/.jpg`，改为按 `media_kinds` 开关放行，并用 `_cap_media()` 限制**每轮媒体数**（默认 2）。不设限的话「读 8 篇」会变成「转写 8 段音频」，一轮跑半小时。
- `_search_media()`：隔轮检索（四个媒体源并行最慢 30-40 秒），媒体候选排在候选池最前——它们数量少，混在几十条网页里会被模型预筛整批刷掉。
- `_extract_all()`：媒体文本阈值从 120 字降到 60 字（图片描述通常比正文短）；**按来源类型选提示词**（见下）。
- 新增 `Engine.ingest_media()`：用户手动贴 URL 的摄取入口，复用同一条提炼/核对/入库管线，与自主学习共用互斥锁。

### 5. 提炼策略：为什么必须换提示词（实测踩坑）

第一版直接复用网页的 `EXTRACT_SYSTEM`，结果**图片摄取稳定入库 0 条**。原因：那套提示词写着「营销文、纯目录页 → 返回空数组」，在模型眼里「对画面的描述」≈ 废话，于是返回 `{"points": []}`。

修法是加一套 `EXTRACT_MEDIA_SYSTEM`：明确「正文是视觉描述或语音转写，与网页正文地位相同」「数量、颜色、布局、坐标、参数、操作顺序都是可入库的事实」，同时保留逐字引用的硬约束。PDF 不换——PDF 的文字是作者写的、可逐字核对，继续用网页版。

### 6. 证据核对放宽到「逐字重叠」（`_evidence_supported()`）

原逻辑要求 evidence 是正文的**严格子串**。中文里模型常在词间多空格，媒体描述又是模型自己写的，严格子串会把合法结论全部拒掉。改成三道由严到宽的检查：

1. 压空白后整句包含（原逻辑）
2. 去掉所有空白再包含
3. evidence 中存在连续 16 字出现在正文中（模型合并相邻两句时用）

**不是放宽到「意思相近」**：连续 16 字逐字一致，改写、翻译、常识补充都过不了。实测「照片里有五只鸟在天空中飞，天空颜色是渐变的」仍被拒。

### 7. 对话链路支持图片（`backend/evolve.py` + `backend/llm.py`）

`answer_with_knowledge(question, topic, images=None)`：图片先用视觉模型读成文字，再拼进问答上下文。**不把图片直接塞给对话模型**——本地主模型不一定有视觉能力，硬塞会 400；「文字模型 + 视觉前端」的组合也能问图。同时给 `llm.chat()` 加了 `images` 参数，将来换视觉对话模型可直接用。

### 8. 配置项（`backend/config.py`，新增 14 个）

```python
"media_enabled": True,                      # 总开关，关掉即回到改造前行为
"media_kinds": ["pdf","image","audio","video"],
"media_per_round": 2,                       # 每轮最多摄取几个媒体
"media_max_mb": 80,                         # 单文件下载上限
"media_timeout": 120, "media_keep_files": True,
"vision_enabled": True, "vision_timeout": 180,
"asr_enabled": True, "asr_model": "small", "asr_device": "auto",
"asr_language": "", "asr_max_seconds": 1800,
"video_subtitles": True, "video_frames": 3,
```
`media_kinds` 做了白名单清洗（写错一个字母只会被忽略，不会让整条管线崩）。

### 9. 接口（`backend/main.py`）

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/media/status` | 能力体检 + 缓存占用 |
| POST | `/api/media/probe` | 不下载，只判类型（含 HEAD 探测） |
| POST | `/api/media/search` | 媒体检索 |
| POST | `/api/media/ingest` | 摄取并入库（后台任务） |
| POST | `/api/media/clear` | 清缓存 |
| POST | `/api/chat` `/api/chat/stream` | 新增 `images` 入参 |

### 10. 界面（`web/`）

新增「多模态」页：四张能力卡（视觉/语音/字幕/PDF，缺什么直接写怎么补）、URL 摄取框、媒体搜索（缩略图 + 一键摄取）、实时进度面板（复用 SSE 事件流）。设置页新增「多模态摄取」卡片，可关闭摄取、改每轮数量与单文件上限、换语音/视觉模型。

---

## 四、实测记录（本机 RTX 4090 / qwen3.8:27b / Windows）

| 场景 | 结果 | 耗时 |
|---|---|---|
| 图片下载（picsum 600×400） | 13.7KB，Content-Type 正确 | 0.79 s |
| 图片视觉解析（本地 PNG 图标） | 三段结构化输出，261 字 | 11.0-14.8 s |
| PDF 文本层（arXiv 15 页） | 15 页正文提取 | 0.5 s |
| PDF 文本层（1 页 sample） | 正常 | 1.1 s |
| YouTube 19 分钟视频（人工中文字幕） | 完整中文转写，**无需 ASR** | 2.8 s |
| 音频转写（115MB / 2 小时英语语音文章） | 405 段带时间戳，`asr_max_seconds=1800` 正确截断在 1799s | 42.5 s（≈43× 实时） |
| 图片全链路（摄取→提炼→核对→入库） | 新增 3 条知识，`source_kind=image` | 26 s |
| 媒体检索（Commons/Openverse/Archive） | 返回直链，可直接摄取 | 2-8 s |
| 查缺补漏（1 个方向，1 个视频来源） | 新增 **9 条**知识，`source_kind=video` | 2 分 53 秒 |

测试与回归：
- 新增 `tests/test_media.py`（38 个用例）：类型判定、字幕三种格式、证据宽严、能力缺失降级、媒体配额、
  图片入问答上下文、查缺补漏的方向清洗与忙时拒绝。
- 项目原有前端测试 `node --test tests/frontend_tests.cjs`：39 项全过。
- 全套 `unittest discover`：223 项全过。
  过程中观察到 2 项间歇性失败（`test_knowledge_dedup` / `test_rotation`，均在本次未改动的模块）：
  根因是 Windows 上 `time.time()` 约 15.6ms 的时钟粒度，使同一批 `created_at` 取到相同值、
  `ORDER BY created_at DESC` 次序不稳定；重跑即通过，与多模态改造无关。

### 依赖踩坑（都已处理，重装环境时要注意）

| 问题 | 现象 | 处理 |
|---|---|---|
| `rapidocr-onnxruntime` 只支持到 Python 3.12 | 3.13 环境装不上，扫描件 OCR 一直不可用 | 按 Python 版本二选一装（3.13 用 `rapidocr` 2.x），`ocr.py` 加了 `_Rapid2Compat` 适配两代 API |
| faster-whisper 调 `av.open(metadata_errors=...)`，PyAV 15+ 删了该参数 | 转写报 `open() got an unexpected keyword argument` | `media._patch_av_open()` 运行时丢掉该参数；旧版 av 在 3.13 上没有 wheel，无法降级 |
| huggingface_hub 默认用符号链接做 blob 去重 | 模型加载报 `WinError 448 无法遍历路径，因为它包含不受信任的装入点` | `_asr_model()` 里设 `HF_HUB_DISABLE_SYMLINKS=1`，改为复制 |
| 沙箱里 `pyvenv.cfg` 的 `home` 指向 Git Bash 模拟符号链接 | `.venv\Scripts\python.exe` 报 `did not find executable` | 把 `home` 改成真实路径 `Q:\...\vendor\python` |

---

## 五、预期效果

### 能力矩阵

| 能力 | 改造前 | 改造后 |
|---|---|---|
| 读网页正文 | ✅ | ✅ |
| 读 PDF（文本层/扫描件） | ❌ | ✅ 文本层 + 扫描件 OCR |
| 读网络图片（画面+图中文字） | ❌ | ✅ 视觉模型，失败退 OCR |
| 读音频（讲座/播客） | ❌ | ✅ 本地转写，带时间戳 |
| 读视频（演示/课程） | ❌ | ✅ 字幕优先，无字幕转写，可抽帧 |
| 自主检索媒体素材 | ❌ | ✅ 4 个免 Key 源 |
| 图片问答 | ❌ | ✅ 图片→文字→RAG 问答 |
| 知识条目带模态 | ❌ | ✅ `source_kind=image/audio/video/pdf` |

### 三个典型场景

1. **ZBrush 教程方向自学**：以前只能读文字帖；现在奇数轮会带回 YouTube 演示视频，2.8 秒拿到中文字幕，把「老师傅口头讲的参数」也提炼成知识。
2. **论文方向自学**：arXiv 摘要页之外，直接吃 PDF 正文（15 页 0.5 秒），不再只依赖摘要。
3. **用户手头有一张参数表截图**：贴进「多模态」页 → 视觉模型转录表格 → 提炼成可检索的知识点，来源标 `image`，点开能看到原图链接。

### 成本与节奏（关键取舍）

- **图片**：11-15 s/张（27B 视觉模型）。换 `qwen2.5vl:7b` 可降到 3-5 s，代价是细节描述略粗。
- **视频**：有字幕 3 s；无字幕需 ASR，10 分钟音频约 1-2 分钟（GPU）。
- **PDF**：文本层几乎零成本；扫描件按页 OCR，约 0.2-0.4 s/页（GPU）。
- 所以默认 `media_per_round=2`、媒体检索隔轮执行——**不让多模态把一轮学习从 2 分钟拖到 30 分钟**。

### 缺依赖时的降级矩阵

| 缺什么 | 仍然可用 | 不可用 |
|---|---|---|
| 视觉模型 | 图片 OCR（文字类截图）、PDF、字幕 | 画面描述、视频抽帧 |
| faster-whisper | 图片、PDF、有字幕的视频 | 无字幕音视频转写 |
| yt-dlp | 直链 mp4/mp3、图片、PDF | YouTube/Bilibili 平台页 |
| PyMuPDF | 图片、音视频 | PDF |

每一项在「多模态」页的能力卡上直接显示为灰色 + 缺什么，不需要翻日志。

---

## 六、怎么用

1. 打开控制台 → 左侧「多模态」，先看四张能力卡。
2. **贴链接摄取**：一行一个 URL（图片/音频/视频/PDF 都行），填一个学习方向，点「开始摄取」。进度在右侧实时滚动。
3. **搜素材**：输关键词，勾选图片/音频/视频，点搜索结果里的「摄取」。
4. **查缺补漏**：见下一节——给**已经学过**的方向补媒体。
5. **自主学习自动带上媒体**：默认开启，每轮最多 2 个媒体来源；不想让它读媒体就在设置里关掉总开关。

依赖已装好（`yt-dlp` / `faster-whisper` / `rapidocr`）。重装环境时按 `requirements.txt` 走，
其中 `av` 与 `rapidocr` 的版本兼容问题已在代码里兜住，见第四节「依赖踩坑」。

---

## 七、用多模态给已有知识查缺补漏

### 知识库现状（决定补漏策略）

| 指标 | 值 |
|---|---|
| 知识点 | 17,187 |
| 方向（主题） | 145 |
| 来源分布 | `book` 16,727 · `qa` 168 · `web` 126 · `documentation` 125 · `wiki` 41 |

也就是说 **97% 的知识来自书库**，方向名形如「书库 · 书名 (作者) (Z-Library)」。
这带来两个后果：

1. 直接拿书名去搜媒体是搜不到的 —— 必须先判断「这本书讲什么」再换成领域关键词。
2. 书里的内容以文字为主，真正缺的是**空间结构、操作顺序、参数在实物上的样子**，
   这些恰恰是图片和视频能补、文字补不了的。

### 一键补漏怎么跑

「多模态」页 →「查缺补漏」卡片：

- **补哪个方向**：下拉列出知识库全部主题；选「全部方向」= 按知识点数量取前 6 个。
- **每个方向媒体数**：默认 2，最多 6。数字越大越慢。
- 点「开始补漏」后，每个方向走一遍：
  1. **诊断媒体缺口**：读该方向最近的 10 条结论，让模型指出「文字讲不清、只能看图/看演示」的缺口，
     产出 2-3 条偏向 tutorial / demonstration / walkthrough 的检索式（实测 2.5-3 秒）。
  2. **检索并去重**：Commons / Openverse / Archive / YouTube 并发检索，
     跳过 `learned_documents` 里已经读过的 URL —— 所以**可以反复点，每次都往更深处走**。
  3. **解析**：视频优先取字幕；图片走视觉模型；无字幕才转写音轨。
  4. **提炼入库**：复用「提炼 → 逐字证据核对 → 去重」管线，`source_kind` 标成 `image/audio/video`。

### 书名类方向的自动改写

方向名会先被清洗成可检索的主题，再交给模型：

```
书库 · ZBrush雕塑艺术完美呈现 (邓威, 温洋, 郑淼) (Z-Library)
   ↓ _query_subject()
ZBrush雕塑艺术完美呈现
   ↓ 模型结合「已掌握的知识」
["ZBrush sculpting workflow demonstration",
 "ZBrush brush technique tutorial",
 "ZBrush character sculpting timelapse"]
```

文学作品同理：`书库 · 一个人在阿拉斯加荒野的25年` →
`["Alaska winter ice river timelapse", "salmon fishing ice edge technique demonstration", ...]`。

### 实测（隔离实例，ZBrush DynaMesh 方向）

```
种子：3 条文字知识（DynaMesh 分辨率、UV 丢失、ZRemesher 拓扑）
补漏：1 个视频来源（"When Should I Use Dynamesh"）
结果：+9 条知识，全部 source_kind=video，耗时 2 分 53 秒
```

补进来的是文字资料里没有的现场信息，例如：

> ZBrush DynaMesh 在极低分辨率设置下适合用于形状阻断（Blocking Out），
> 能强制用户关注主要形状而非细节。

> ZBrush DynaMesh 可用于快速执行布尔运算，包括将多个几何体合并（并集）
> 或通过按住 Alt 键插入几何体来实现减法布尔。

### 建议的跑法

- **先挑技术类方向**：ZBrush / 3D 打印 / 软件操作这类，媒体命中率最高。
  纯小说方向也能补（作者访谈、改编影像、故事发生地实景），但价值密度低。
- **一次 1-2 个方向**，每个方向 2 个媒体来源。145 个方向一次全跑要几小时。
- **反复点**：已读媒体会跳过，第二轮会自动换检索角度，越补越深。
- 补完在「知识库」按方向筛选，来源标签会显示 `video` / `image`，点开能看到原始链接。

### 也可以手动指定

不想让模型自己找，就在「摄取网络媒体」里贴具体链接（比如你已知某个教学视频/某张参数表截图），
填同一个方向名，走的是同一条提炼入库管线，只是来源由你决定。

---

## 八、已知边界

- **YouTube 反爬**：字幕通道稳定；无字幕时抓音轨可能被要求登录或 PO Token，失败会明确报「取不到音轨」。
- **视觉模型是"读图"不是"理解专业内容"**：图表能读坐标轴与趋势，但把工程图读成精确尺寸不现实；结论仍要过证据核对，读不出来就返回空，不编。
- **ASR 幻觉**：已用 `vad_filter` + `condition_on_previous_text=False` 抑制，长音频仍需人工抽查。
- **磁盘**：媒体缓存在 `data/media`，可在界面清空；`media_keep_files=False` 可改为解析完即删。
- **版权**：Commons/Openverse 结果带 license 字段，界面显示；平台视频只取字幕/音轨用于本地学习，不对外分发。
