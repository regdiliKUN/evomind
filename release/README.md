# 发行包 · EvoMind · 自进化学习引擎

本目录存放 **EvoMind · 自进化学习引擎** 的 Windows x64 可运行包体（由桌面快捷方式实际指向的安装/发布目录整理而来）。

| 项目 | 内容 |
| --- | --- |
| 版本 | `1.0.0` |
| 平台 | Windows x64 |
| 包体路径 | `release/1.0.0/evomind-1.0.0-win-x64/` |
| 文件数 | 121 |
| 体积 | 约 17.6 MB |

## 运行方式

双击 `EvoMind.exe`

## 校验完整性

```bash
cd release
sha256sum -c SHA256SUMS.txt
```

Windows PowerShell：

```powershell
Get-Content release\SHA256SUMS.txt | ForEach-Object {
  $h, $p = $_ -split '  ', 2
  if ((Get-FileHash $p -Algorithm SHA256).Hash -ne $h.ToUpper()) { "MISMATCH $p" }
}
```

## 关于 EvoMind · 自进化学习引擎

会自己上网学习、越学越强的本地 AI：自主检索 → 知识入库 → LoRA 微调，三层进化。

## 未纳入的内容

开发环境 `.venv/`、`build/`，以及 `data/`（含模型权重、微调产物与知识库）未纳入。

以上目录属于开发环境、运行期缓存或大体积模型权重，不影响程序本体运行。
