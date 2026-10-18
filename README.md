# EvoMind · 自进化学习引擎

<p align="center">
  <img src="assets/icon.png" width="112" alt="EvoMind">
</p>

<p align="center">
  一个会自己上网学习、并且越学越强的本地 AI。<br>
  你定方向，它自己检索 → 阅读 → 提炼 → 入库 → 微调，全程不需要你盯着。
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10+-blue" alt="python">
  <img src="https://img.shields.io/badge/platform-Windows-lightgrey" alt="platform">
  <img src="https://img.shields.io/badge/inference-Ollama-green" alt="ollama">
  <img src="https://img.shields.io/badge/license-MIT-brightgreen" alt="license">
</p>

---

## 它解决什么问题

大多数「本地 AI」是静态的：模型训练完就固定了，你不知道它懂什么、不懂什么，也没法让它变强。

EvoMind 把它反过来：**你只给一个方向，剩下的它自己干**。它知道自己缺什么，会主动去补，补完存下来，攒够了再把知识压进模型权重。

## 三层进化

| 层 | 做什么 | 见效 |
|---|---|---|
| **知识层（RAG）** | 自主检索 → 抓正文 → LLM 提炼知识点 → 语义去重入库 | 几分钟 |
| **权重层（LoRA）** | 知识库导出 → 扩写成问答对 → LoRA 微调 → 合并权重 | 几小时 GPU |
| **资源层** | 识别 HF 模型/数据集 → 判断相关性 → 预算内自主下载 | 学习时顺带 |

## 为什么它不会原地打转

普通爬虫是「搜关键词 → 存起来」，搜完就没了。

EvoMind 每轮都会把 **「已掌握的知识」+「已用过的检索式」** 一起喂回模型，强制它计算新的知识空白。所以：

```
第 1 轮  →  建立全景框架和核心概念
第 3 轮  →  进入机制原理与量化细节
第 6 轮  →  挖边界条件、工程实践、争议点
```

反思阶段产出的「下一步该深挖什么」会直接变成下一轮的最高优先级缺口。

## 实测效果

一次真实运行（RTX 4090 / qwen3.8:27b）：

```
方向：RAG 系统的检索质量优化与重排序
3 轮 / 93 秒 → 31 条知识，24 篇资料
```

学到的内容是这种粒度的：

> 在医疗程序重排序任务中，经过 ListNet 微调的 109M 参数 Cross-Encoder 在 NDCG@3 上比 4B 参数 LLM 重排序器高 2.6 个百分点。

问它「小型 Cross-Encoder 和大型 LLM 该选哪个」，它会分场景回答并标注 `[1][2][3]` 引用；问到没学过的内容，它直接说「我目前学到的还不够回答这部分」，**不编**。

## 快速开始

### 前置

- **Ollama**（[下载](https://ollama.com/download)）
- **Python 3.10+**
- 建议 16GB+ 显存（8GB 也能跑小模型）

```bash
ollama pull qwen3.8:27b     # 或任意你喜欢的模型
ollama pull bge-m3          # 向量模型
```

### 安装与启动（Windows）

**第一次用**：双击 **`⓪ 首次安装.cmd`**

它会依次完成：建虚拟环境 → 装依赖 → 检查 Ollama → **问你要不要在桌面放一个 EvoMind 图标**（可以选不要）。

**以后每次**：双击 **`① 双击这里启动.cmd`**，或者直接双击桌面上的图标。

浏览器会自动打开控制台，约 10 秒。依赖出问题时跑 **`② 修复环境.cmd`**。

### 桌面图标

安装时会问一次，**选「不要」也完全不影响使用**。

之后想加或者想删，随时双击 **`③ 桌面图标.cmd`** —— 它会先告诉你现在有没有，再问你要创建还是删除。

图标长这样（与网页控制台同一套视觉）：

<p align="center">
  <img src="assets/icon.png" width="72" alt="icon">
</p>

> 实现上走 `.lnk` + `WScript.Shell`，会正确解析被重定向过的桌面目录（比如桌面设在 D 盘），
> 不会傻傻地往 `C:\Users\xxx\Desktop` 里塞文件。

### 手动启动（跨平台）

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python run.py
```

默认地址 `http://127.0.0.1:8765`，端口被占会自动顺延。

## 架构

```
                    ┌──────────────┐
   用户设定方向  →  │  进化引擎     │
                    └──────┬───────┘
                           │
        ┌──────────────────┼──────────────────┐
        ↓                  ↓                  ↓
   ① 诊断知识缺口     ② 多源检索        ③ 抓取正文
   （读已有知识，      DDG / arXiv        trafilatura
     找空白）          GitHub / Wiki      提取主内容
        │              HuggingFace             │
        └──────────────────┼──────────────────┘
                           ↓
                   ④ LLM 提炼知识点
                   （claim + 出处 + 可信度）
                           ↓
                   ⑤ 语义去重入库
                   （bge-m3 + 余弦相似度）
                           ↓
        ┌──────────────────┼──────────────────┐
        ↓                  ↓                  ↓
   ⑥ 资源发现         ⑦ 反思下一轮      ⑧ 持续自学
   预算内自主下载     产出新缺口        空闲后自动开下一轮
```

### 代码结构

```
evomind/
├── backend/
│   ├── config.py       配置（data/config.json 热加载）
│   ├── llm.py          Ollama 封装：对话 / 结构化输出 / 向量化
│   ├── search.py       5 个免 Key 检索源，并发 + 去重
│   ├── fetcher.py      正文提取（trafilatura），arXiv/GitHub 专用路径
│   ├── memory.py       SQLite WAL + numpy 余弦检索 + 语义去重
│   ├── evolve.py       进化主循环（本项目的核心）
│   ├── downloader.py   体积估算 → 预算闸门 → HF 下载 → GGUF 注册
│   ├── finetune.py     数据集构建 + bf16 LoRA 训练 + 合并
│   ├── scheduler.py    持续自学调度
│   └── main.py         FastAPI + SSE 事件流
├── web/                控制台（原生 JS，无构建步骤）
├── scripts/
│   ├── install.py      首次安装：建环境 → 装依赖 → 问桌面图标
│   ├── shortcut.py     桌面快捷方式创建/删除（.lnk + WScript.Shell）
│   └── desktop_icon.py 图标管理交互
├── assets/
│   ├── icon.ico        应用图标（7 个尺寸：16~256）
│   ├── icon.png        512px 预览图
│   └── make_icon.py    图标生成脚本（Pillow + numpy）
├── data/               运行时数据（不入库）
├── run.py              启动器：拉起 Ollama → 起服务 → 开浏览器
├── requirements.txt
├── ⓪ 首次安装.cmd
├── ① 双击这里启动.cmd
├── ② 修复环境.cmd
├── ③ 桌面图标.cmd
└── 使用说明.html        面向非技术用户的完整说明
```

## 自主下载的四道闸门

它确实有权限自己下载东西，但**不可能把硬盘塞爆**：

1. **单体积上限** — 默认 40GB，超过直接跳过
2. **总预算** — 默认 200GB，累加超过就停
3. **磁盘余量** — 下载前检查剩余空间，不足 1.15 倍就跳过
4. **相关性筛选** — 先让模型判断「这东西对当前方向有没有用」，没用不下

下载的 GGUF 会自动 `ollama create` 注册，可在设置里直接切换使用。

## 配置

所有参数在网页「设置」页改，写进 `data/config.json`，**无需重启**。

几个值得调的：

| 参数 | 默认 | 说明 |
|---|---|---|
| `queries_per_round` | 4 | 每轮生成几个检索式 |
| `fetch_per_round` | 8 | 每轮读几篇 |
| `min_relevance` | 0.45 | 知识点入库门槛，调到 0.6 只收强相关 |
| `dedup_threshold` | 0.93 | 语义去重阈值，调到 0.88 更宽容 |
| `idle_minutes` | 20 | 持续自学的间隔 |
| `disk_budget_gb` | 200 | 资源总预算 |

## 权重层（微调）

控制台「权重进化」页，四步：

1. 装微调环境（约 3GB：PyTorch + Transformers + PEFT）
2. 知识库攒够几百条
3. 生成训练集（自动把陈述改写成多样化问答对）
4. 开始微调

默认走 **bf16 LoRA + gradient checkpointing**，不依赖 bitsandbytes（Windows 上更稳），7B 模型峰值约 17GB 显存。

> 样本质量决定成败。建议先去知识库删掉明显没用的内容再训。

## 已知限制

- **只在 Windows 上测过**，其他平台理论上可用但未验证
- **桌面图标仅 Windows 有效**（走 `.lnk` + `WScript.Shell`），其他系统安装器会自动跳过
- 检索源全部免 Key，因此**有速率限制**，高频学习可能被临时限流
- 第 3 轮之后出现「0 条新知识」是正常的 —— 说明该方向已学透，该换角度了
- 微调不是越大越好，几百条样本容易过拟合，建议小步多次

## License

MIT
