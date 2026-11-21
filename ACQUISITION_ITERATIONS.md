# 知识获取效率 · 20 轮迭代记录

> 起点：`ACQUISITION.md` 记录的第一轮改造（摄取台账 + 段落过滤 + 入库重写）。
> 终点：本文件末尾的最终版本。
> 约束：核心需求（提高知识获取效率）与整体结构不变，每轮只动一处可验证的地方。

每轮的格式固定：**目标 → 改动 → 理由 → 自检**。自检结果都是跑出来的，不是估计。

---

## 第 1 轮 · 台账查询走覆盖索引

**目标（性能）**：台账涨到十万级后，每轮选资料的那次「这批 URL 读过没有」不能退化成全表扫。

**改动**：`document_attempts` 的索引从 `(topic, last_at)` 换成 `(topic, url, last_at)`；旧索引在 `_upgrade` 里 DROP。新增 `tests/bench_attempts.py` 做 A/B。

**理由**：查询形状是 `topic=? AND url IN (...) AND last_at>=?`。旧索引只能吃到前两列，URL 要回表逐行比；新索引三列全覆盖，`EXPLAIN QUERY PLAN` 显示 `COVERING INDEX`，连回表都没有。

**自检**：20 万行 / 8 个方向（每方向 2.5 万行，真实形状）：

| 索引 | 中位耗时 | 执行计划 |
|---|---:|---|
| `(topic,last_at)` | 54.93 ms | SEARCH … INDEX idx_attempts_topic |
| `(topic,url,last_at)` | **0.13 ms** | SEARCH … **COVERING** INDEX idx_attempts_lookup |

**422 倍**。目标达成。

---

## 第 2 轮 · 台账写入失败不再拖垮整轮

**目标（正确性）**：台账是加速手段，写失败不能连累本轮已经提炼出来的知识点。

**改动**：新增 `Engine._record_attempts()`，内部 try/except，失败只发一条 `warn`；三处调用点全部改走它。

**理由**：原来 `memory.record_attempts()` 直接调用，数据库被锁或磁盘满时异常会穿过 `_extract_all` → `_round` → `_run`，把整轮标成 error——而这一轮的知识点其实已经入库了。

**自检**：用例 `test_ledger_failure_does_not_discard_extracted_points`：把 `record_attempts` 打成必然抛 `database is locked`，`_extract_all` 仍返回 1 条知识点。通过。

---

## 第 3 轮 · 「已学」与「已读」合并成一次事务

**目标（一致性 + 性能）**：两张表要么都写、要么都不写；顺带去掉每篇一次 commit。

**改动**：`record_attempts(..., learned=[...])` 在一个事务里同时写 `document_attempts` 与 `learned_documents`；`_extract_all` 里删掉 `for url in good: memory.mark_learned(...)` 的循环。

**理由**：分两次提交时进程若在中间退出，会留下「台账说读过了、已学表却没有」的错位——之后这篇资料既不会被跳过、也不被算作已学，跳过逻辑和统计各看到一半事实。同时一轮 8 篇就是 8 次 commit。

**自检**：`test_both_tables_are_written_in_one_call` 断言两表一致、重复记录只累加 `attempts`；全量用例（含 `test_document_completion_is_scoped_to_topic`）保持通过。

---

## 第 4 轮 · 提炼批次的异常隔离

**目标（健壮性）**：一个坏段落不该吞掉整轮已完成的提炼结果。

**改动**：`ThreadPoolExecutor.map` 换成 `submit` + `as_completed`，每个 future 单独 try/except。

**理由**：`ex.map` 在迭代时遇到任何一个 worker 抛异常就中断整轮——前面已经提炼好的段落全部作废。而 `one()` 内部虽然 catch 了模型异常，但文档结构异常（缺字段、类型不对）不在其覆盖范围内。

**自检**：`test_worker_exception_keeps_other_sections` 把第二个段落换成字符串（`doc.get` 必炸），第一条仍然入库。通过。

---

## 第 5 轮 · 核对失败拆半重试

**目标（完整性/产出率）**：一次偶发失败不该让 12 条候选全部作废。

**改动**：`_verify_points` 拆出 `_verify_batch(batch, depth)`；失败且 `len(batch)>1` 时对半再试一次，仍失败才放弃并明确报出丢弃条数。

**理由**：候选是花过一次提炼调用换来的。整批 12 条一起核对时，输出偏长被截断或偶发超时都会让 12 条同时作废。拆半只多花一次调用，通常就能绕开「输出太长」。

**自检**：`test_transient_failure_splits_instead_of_dropping_batch`：第 1 次调用抛错、之后正常 → 12 条全保，共 3 次调用（1 失败 + 2 拆半）。对比改动前：12 条全丢。

---

## 第 6 轮 · 每轮获取漏斗

**目标（完整性/可观测性）**：把「检索→预筛→抓取→段落→候选→核对→入库」摊开，效率丢在哪一步要能看见。

**改动**：`Engine.FUNNEL_KEYS` + `_bump()`；`_round` 包一层 `try/finally` 保证提前返回（候选读尽、没有检索式）也输出漏斗；`state["funnel"]` 随 `/api/status` 暴露。

**理由**：此前只有零散日志，用户看到「本轮 0 条」无法判断是检索没命中、抓取失败、提炼为空还是核对驳回。漏斗把 8 个数字放在一条事件里。

**自检**：`test_round_funnel_counts_each_stage` 断言 sections/candidates/verified/added 四项计数；提前返回路径由 `_round` 的 finally 覆盖（`test_idle_rounds_stop_early` 顺带验证不抛错）。

---

## 第 7 轮 · 中文词切分统一口径

**目标（正确性/产出质量）**：排序偏了，好资料会被挤出预筛窗口。

**改动**：`search.cjk_terms()`——滑动二元组 + 虚词过滤；`rank_results` 与 `evolve._cjk_bigrams` 共用它（后者改为委托）。

**理由**：原来用 `re.findall(r"[\u4e00-\u9fff]{2}", query)`，不重叠切分。「ZBrush：衣褶的受力点与主次关系」被切成 `衣褶/的受/力点/与主/次关`——「的受」「与主」是纯噪音，既抬高无关页面的分数，又漏掉「受力」「主次」这些真实组合。

**自检**：同一无关页（「3D 打印与主机配置指南」）在旧口径下靠「与主」命中得 0.2 分进入候选池，新口径 0 命中被直接剔除；相关页仍排第一。用例 `test_irrelevant_page_no_longer_scores` 固化。

---

## 第 8 轮 · 媒体检索短缓存

**目标（性能）**：同一组参数 10 分钟内不重复打 4 个外部源。

**改动**：`search_media` 加 `(query, kinds, sources)` 键的 TTL 缓存（600s，上限 64 条，简单淘汰）；`media_cache_clear()` 供测试与配置变更使用。

**理由**：一次 `search_media` 要并行打 Commons/Openverse/Archive/YouTube，最慢 30-40 秒；学习循环隔轮就用同一个 query 再查一次，补漏还会对同一方向连查 3 个检索式。

**自检**：`test_repeated_media_query_hits_cache`：连续两次同参查询只触发 1 次源调用，换 query 后变成 2 次。`test_clear_resets_cache` 验证清空有效。

---

## 第 9 轮 · 「已学」判断批量化

**目标（性能）**：一轮 60 个候选不该打 60 次库。

**改动**：新增 `memory.docs_learned(urls, topic)`（一次 `IN` 查询，按 500 分批）；`_pick_targets` 与 `_pick_media_targets` 改用它，`doc_seen()` 保留给单条场景。

**理由**：第 1 轮只批量化了台账查询，`doc_seen(url, topic)` 仍在循环里逐条调用——典型的 N+1。

**自检**：60 个候选（24 个已学）下统计 SQL：

| 写法 | SELECT 次数 |
|---|---:|
| 批量台账 + 逐个 `doc_seen` | 61 |
| 批量台账 + 批量已学 | **2** |

用例 `test_candidate_filtering_uses_bounded_queries` 断言 ≤3 次，防止以后又写回 N+1。

---

## 第 10 轮 · 空转熔断

**目标（效率）**：资料读尽后，不要用剩下的几十轮反复「检索→无新资料」。

**改动**：`STALL_LIMIT = 3`；`_run` 里跟踪连续「既没读到新资料、也没新增知识」的轮数，达到上限且还有剩余轮次就提前结束，并说明原因与已读篇数。

**理由**：这是本轮最直接的浪费——某个方向 100 轮里后 90 轮都在做同样的事。每轮仍要付一次规划调用 + 两轮检索扇出（4 条检索式 × 6-8 个源）。

**自检**：`test_idle_rounds_stop_early`：100 轮配置下只跑 3 轮；`test_reading_something_keeps_going`：每轮都读到 1 篇时跑满 5 轮（不会误杀）；`test_exactly_at_limit_with_no_rounds_left_is_fine`：正好 3 轮时不报「提前结束」。

---

## 第 11 轮 · 补台账的时间窗与上限

**目标（边界/启动性能）**：老库补台账不能把第一个请求拖住，也不能写入永远不会被查到的死数据。

**改动**：`_backfill_attempts` 只补 `fetched_at >= now - doc_retry_days` 的记录，并 `LIMIT 20000`。

**理由**：窗口外的记录补进去，查询时也会因为 `last_at` 太旧被过滤——纯属往库里写死数据；而无上限的 `INSERT…SELECT` 在大库上会让建连接等上几秒。

**自检**：真实库副本（976 条 documents）补进 476 条（窗口内），耗时 7.3ms（预热配置后）。用例 `test_backfill_only_takes_recent_documents` 固化「90 天前的不补」。

顺带发现：给补写加 `ORDER BY fetched_at DESC` 会强制全表排序，976 行就从 7ms 涨到 63ms——已去掉排序，只留 LIMIT。

---

## 第 12 轮 · 台账的长期增长边界

**目标（边界情况）**：只增不减的表早晚拖慢每次建连接。

**改动**：`prune_attempts(keep_days=180)` + `_daily_maintenance()`：每天最多清 5000 行（`rowid IN (SELECT … LIMIT 5000)`），用 `maintenance` 表记录上次执行时间。

**理由**：先写的版本是「一次删干净」，20 万行要 186ms，直接压在第一个请求上。改成有上限的批量清理后，历史积压分几天清完，每天只花几毫秒。

**自检**：20 万行库上连续三天各清 6.7 / 6.3 / 6.6 ms，每天 5000 行；同日第二次调用 0.02ms（被 `maintenance` 拦住）。用例覆盖「只删过期」与「每天最多一次」。

---

## 第 13 轮 · 获取效率接口

**目标（完整性）**：效率数据不能只躺在日志里。

**改动**：`memory.acquisition_report()`（读过多少篇、产出多少篇、产出率、各方向明细）+ `GET /api/analytics/acquisition`。

**理由**：漏斗说明「这一轮丢在哪一步」，产出率说明「长期看哪些方向在读白书」。两者配合才能定位问题：产出率低 → 检索式或预筛偏了，而不是模型不行。

**自检**：`test_endpoint_reports_yield_rate`：2 篇读过、1 篇有产出 → `yield_rate = 0.5`，`retry_days` 与配置一致，`topics` 按篇数排序。

---

## 第 14 轮 · 为前 13 轮补回归用例

**目标（正确性）**：每一处改动都有对应的失败模式被钉住。

**改动**：`tests/test_acquire_efficiency.py` 从 15 项扩到 44 项，新增 8 个测试类：台账规模（1200 条跨 SQL 变量上限）、台账健壮性、核对重试、媒体缓存、查询次数上界、熔断、漏斗、中文词、接口、维护任务、向量缓存、预筛窗口、并发写入、设置项一致性。

**理由**：这些改动大多是「边界 + 降级」，正常路径跑一遍看不出问题，必须构造失败才能验证。

**自检**：44 项全绿；全量 267 项通过（改动前 238 项）。

---

## 第 15 轮 · 提炼过滤抽成纯函数

**目标（可读性/可测试性）**：`_extract_all` 里的 `one()` 有 70 行嵌套闭包，其中一半是纯过滤逻辑。

**改动**：抽出模块级 `_points_from(data, doc, text, min_rel)`，四道关（结构/长度/证据/相关度）集中在一处；`one()` 缩到 30 行。

**理由**：这是「产出率」最直接的抓手——同一段模型输出，过滤口径变了产出就变。抽出来就能脱离线程池、脱离数据库单独测，也避免以后改口径时在两处漂移。

**自检**：重构后 267 项用例全绿（行为等价）；函数本身由第 14 轮的用例间接覆盖（证据不足、相关度不足、claim 过短）。

---

## 第 16 轮 · 入库写入的剩余开销

**目标（性能）**：240 条（一本书）的写入还有明显浪费。

**改动**：`INSERT` 后用游标的 `lastrowid` 取代 `SELECT last_insert_rowid()`；`_to_blob(vectors[i].tolist())` 改为 `_to_blob(vectors[i])`。

**理由**：剖析显示 240 条入库里，481 次 `execute` 有一半是每插一行再查一次 rowid；而 `vectors[i]` 本来就是 `float32` ndarray，`.tolist()` 先转成 1024 个 Python float 再由 `np.asarray` 转回去，纯浪费。

**自检**：

| 批量 | 改动前 | 改动后 |
|---|---:|---:|
| 240 条 | 114.1 ms | **74.2 ms** |
| 30 条 | 19.4 ms | **17.4 ms** |

---

## 第 17 轮 · 向量化结果缓存

**目标（性能/边界）**：同一句话不要重复向量化。

**改动**：`llm.embed` 加文本级 LRU（256 条，返回副本），并把 `_embed_remote` 抽出来；同一批里重复的文本合并成一次请求；`embed_cache_clear()` 供测试隔离。

**理由**：学习循环每轮都用同一个「方向」字符串检索一次（`_plan` → `memory.search`），而检索结果缓存每次入库后失效，于是每轮都重新向量化同一个句子。24GB 卡上这只是 7ms；显存小到只能驻留一个模型时，它会把 27B 挤下去、下一次对话再重新加载（20-40 秒）。

**自检**：`test_repeated_text_is_embedded_once`（两次调用只打 1 次远端）、`test_duplicate_texts_in_one_batch_are_merged`（`["a","b","a"]` → 请求 `["a","b"]`）、`test_cache_is_bounded`、`test_returned_vectors_are_copies`（调用方改返回值不污染缓存）。

---

## 第 18 轮 · 把重读间隔接进设置面板

**目标（完整性）**：错误提示让用户「调大重读间隔」，界面却够不着，等于没说。

**改动**：`web/index.html` 加「资料重读间隔（天）」输入 + 说明；`web/app.js` 读取与保存 `doc_retry_days`。

**理由**：这是本轮唯一新增的配置项，也是唯一需要用户自己权衡的旋钮（省时间 vs 发现更新）。只写进文档、不给入口，实际使用率接近零。

**自检**：`node --check web/app.js` 通过；前端 39 项用例通过；新增 `test_retry_setting_exists_in_both_html_and_js` 防止「HTML 有输入框、JS 不保存」这类静默失效。

---

## 第 19 轮 · 边界与一致性收口

**目标（边界情况 + 一致性）**：三处「看起来能用、边界上不对」的地方。

**改动**：

1. `_pick_targets` 的预筛窗口从硬编码 24 改成 `max(24, fetch_per_round)`，校验上界同步；
2. `attempted_urls(days<=0)` 显式返回空集（表示「关掉台账」），不再依赖 `since=now` 的巧合；
3. `record_attempts` 的写入纳入 `memory._lock`，与其他写入串行化。

**理由**：`fetch_per_round` 允许配到 50，但预筛只看前 24 条——配额 50 永远选不满；`days=0` 靠时钟精度「碰巧」查不到，换台机器可能漏一条；两个线程同时开事务时 SQLite 会报 `database is locked`，虽然第 2 轮已降级，但没必要让它频繁失败。

**自检**：`test_prefilter_window_covers_configured_quota`（配额 30 时第 30 条能被选中）、`test_concurrent_ledger_writes_do_not_fail`（4 线程 × 20 次写入，0 异常，80 条台账）、`test_ledger_is_scoped_to_topic_and_expires`（`days=0` 返回空）。

---

## 第 20 轮 · 文档、记录与最终验证

**目标（可读性/完整性）**：让这一轮的结论可复现、可追溯。

**改动**：本文件 + `ACQUISITION.md` 数据更新 + `README.md` 小节更新；三条基准脚本纳入仓库（`bench_acquire.py` / `bench_ingest.py` / `bench_attempts.py`）。

**理由**：没有复现命令的性能数字等于没有数字。

**自检**：见文末「最终验证」。

---

## 最终验证

| 检查 | 结果 |
|---|---|
| `python -m unittest discover -s tests -p "test_*.py"` | **267 项通过**（本轮起点 238 项，新增 29 项） |
| `python tests/run_tests.py --fast` | 68 项通过，0 失败，2 个网络跳过 |
| `node --test tests/frontend_tests.cjs` | 39 项通过 |
| `node --check web/app.js` | 通过 |
| 真实库副本跑迁移 | 476 条台账，7.3 ms，二次连接不重复 |
| 真实 bge-m3 向量链路 | 3 条入库、重复判重、检索 top1 正常 |

## 最终版本相对初始版本的整体提升

初始版本 = 第一轮改造完成时（台账 + 段落过滤 + 入库重写，238 项用例）。

| 维度 | 初始版本 | 最终版本 | 说明 |
|---|---:|---:|---|
| 5 会话 × 3 轮的抓取/提炼次数（同样 3 条知识） | 108 | **24** | 第 1 轮改造带来的主收益，本 20 轮保持不退化 |
| 30 条批量入库 | 19.4 ms | **17.4 ms** | 第 16 轮 |
| 240 条批量入库（一本书） | 114.1 ms | **74.2 ms** | 第 16 轮，比改造前 615.5 ms 快 8.3× |
| 台账查询（20 万行 / 8 方向） | 54.93 ms | **0.13 ms** | 第 1 轮，422× |
| 选资料阶段的 SQL 次数（60 候选） | 61 | **2** | 第 9 轮 |
| 核对偶发失败时的候选损失 | 12 条 / 次 | **0 条** | 第 5 轮 |
| 单段落异常对整轮的影响 | 整轮中断 | **仅丢该段** | 第 4 轮 |
| 台账写失败对整轮的影响 | 整轮报错 | **仅一条警告** | 第 2 轮 |
| 资料读尽后的空转轮次 | 跑满配置 | **3 轮后熔断** | 第 10 轮 |
| 台账长期增长 | 无上限 | **180 天 / 每天 5000 行** | 第 12 轮 |
| 每轮可观测性 | 零散日志 | **8 段漏斗 + 产出率接口** | 第 6、13 轮 |
| 配置项可调性 | 只能改 JSON | **设置面板可改** | 第 18 轮 |
| 回归用例 | 238 | **267** | 第 14 轮为主 |
