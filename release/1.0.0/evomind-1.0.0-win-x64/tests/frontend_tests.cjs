// Runs the actual console script with a minimal DOM; no npm packages or server required.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function consoleApp(fetch) {
  const nodes = new Map();
  const element = () => {
    const el = {
      value: "", dataset: {}, style: {}, className: "", textContent: "", _html: "", writes: 0,
      children: [], scrollTop: 0, scrollHeight: 0,
      listeners: {}, addEventListener(name, fn) { el.listeners[name] = fn; },
      classList: { toggle() {} },
      attrs: {}, setAttribute(k, v) { el.attrs[k] = String(v); }, getAttribute(k) { return el.attrs[k]; },
      appendChild(c) { el.children.push(c); el._html += c._html || ""; return c; },
      removeChild(c) { const i = el.children.indexOf(c); if (i >= 0) el.children.splice(i, 1); return c; },
      remove() {},
      // 只支持测试用到的 ".empty" 探测：命中就从 _html 里摘掉该占位
      querySelector(sel) {
        if (sel === ".empty" && el._html.includes("empty")) {
          const e = element();
          e.remove = () => { el._html = el._html.replace(/<div class="empty">[\s\S]*?<\/div>/, ""); };
          return e;
        }
        return null;
      },
    };
    Object.defineProperty(el, "firstChild", { get() { return el.children[0]; } });
    // 复制按钮读 innerText：用去标签后的文本近似即可
    Object.defineProperty(el, "innerText", { get() { return el._html.replace(/<[^>]+>/g, ""); } });
    // innerHTML 走 setter：测试据此判断「这一轮到底有没有重建 DOM」。
    // 置空（""）时同步清掉 children，模拟真实 DOM 的替换语义。
    Object.defineProperty(el, "innerHTML", {
      get() { return el._html; },
      set(v) { el._html = v == null ? "" : String(v); el.writes++; if (!v) el.children = []; },
    });
    return el;
  };
  const document = {
    hidden: false,
    addEventListener() {},
    querySelector(selector) {
      if (!nodes.has(selector)) nodes.set(selector, element());
      return nodes.get(selector);
    },
    querySelectorAll: () => [],
    createElement: element,
  };
  // EventSource 记为可控实例：测试据此手动触发 onopen / onmessage / onerror
  const esRef = { current: null };
  const copied = [];
  const ctx = vm.createContext({
    document, fetch, URLSearchParams, console, Date,
    navigator: { clipboard: { writeText: async (v) => {
      if (copied.fail) throw new Error("denied");
      copied.push(v);
    } } },
    location: { hash: "#chat" }, window: { addEventListener() {} },
    EventSource: class {
      constructor(url) { this.url = url; esRef.current = this; }
      addEventListener() {} close() {}
    },
    setInterval() {}, setTimeout() {}, clearInterval() {}, clearTimeout() {},
  });
  const source = fs.readFileSync(path.join(__dirname, "../web/app.js"), "utf8");
  vm.runInContext(
    source + "\nglobalThis.app = {state, loadKnowledge, refreshAll, loadAnalytics, renderMilestones, loadDashboard, appendDashEvent, renderLibDirs, renderLibDirSummary, refreshDirections, startLearning, loadRefinements, paintAuto, startRefinement, paintRefinement, paintRotation, paintRotationHistory, renderRotationSummary, loadRotation, fillRotationSettings, previewSimilarKnowledge, confirmSimilarCleanup, clearSimilarPreview, loadDuplicates};",
    ctx
  );
  return { app: ctx.app, document, nodes, es: esRef.current, copied };
}

const response = (items = []) => ({ ok: true, json: async () => ({ items, total: items.length }) });

test("random choice leaves learning manual and preserves sixteen rounds", async () => {
  const learnBodies = [];
  const { app, document } = consoleApp(async (url, opts) => {
    if (url === "/api/learn/directions") return { ok: true, json: async () => ({ items: ["ZBrush：DynaMesh", "ZBrush：ZRemesher"] }) };
    if (url === "/api/learn/start") { learnBodies.push(JSON.parse(opts.body)); return { ok: true, json: async () => ({ ok: true }) }; }
    return response();
  });
  document.querySelector("#direction-seed").value = "ZBrush";
  document.querySelector("#rounds").value = "16";
  await document.querySelector("#btn-random-learn").listeners.click();
  assert.equal(learnBodies.length, 0, "random choice must not start a learning session");
  assert.match(document.querySelector("#direction").value, /^ZBrush：/);
  assert.equal(document.querySelector("#rounds").value, "16");
  assert.equal(!!document.querySelector("#btn-start").disabled, false);
  await app.startLearning();
  assert.equal(learnBodies[0].rounds, 16);
});

test("one-click books ignores topic filters and guards repeated clicks", async () => {
  let resolve, submissions = 0;
  const { app, document } = consoleApp(async (url, opts) => {
    if (url === "/api/refinements/books/start") {
      submissions++;
      assert.equal(opts.method, "POST");
      assert.equal(opts.body, undefined);
      return new Promise((r) => { resolve = r; });
    }
    if (url.startsWith("/api/refinements?")) return { ok: true, json: async () => ({ items: [], total: 0, state: { running: true, mode: "books", total: 500, done: 40, skipped: 40 } }) };
    return response();
  });
  document.querySelector("#kb-topic").value = "some selected book";
  const first = app.startRefinement(true);
  await app.startRefinement(true);
  resolve({ ok: true, json: async () => ({ ok: true, total: 500, pending: 460, skipped: 40 }) });
  await first;
  assert.equal(submissions, 1);
  assert.equal(document.querySelector("#kb-topic").value, "");
  assert.equal(document.querySelector("#btn-refine-books").disabled, true);
  assert.equal(document.querySelector("#refine-progress").value, 8);
});

test("book refinement progress distinguishes saved, skipped, stopped and failed work", () => {
  const { app, document } = consoleApp(async () => response());
  app.paintRefinement({ running: false, mode: "books", stage: "stopped", total: 200, done: 80, skipped: 60, saved: 1 });
  assert.match(document.querySelector("#refine-status").textContent, /再次一键可继续/);
  assert.match(document.querySelector("#refine-status").textContent, /80\/200/);
  assert.equal(document.querySelector("#refine-progress").value, 40);
  assert.equal(document.querySelector("#btn-refine-books").disabled, false);
  app.paintRefinement({ running: false, mode: "books", stage: "partial", total: 200, done: 200, failed: 2 });
  assert.match(document.querySelector("#refine-status").textContent, /2 批失败/);
});

test("refinement history displays book names and exposes older result pages", async () => {
  const { app, document } = consoleApp(async () => ({ ok: true, json: async () => ({ total: 45,
    state: { running: false }, items: [{ topic: "shared topic", book_title: "Book <A>",
      input_count: 60, items: [], sources: [], before_chars: 1000, after_chars: 100, created_at: 1 }],
  }) }));
  await app.loadRefinements();
  assert.match(document.querySelector("#refine-results").innerHTML, /Book &lt;A&gt;/);
  assert.match(document.querySelector("#refine-page").textContent, /1\/3页/);
  assert.equal(document.querySelector("#btn-refine-next").disabled, false);
});

test("direction refresh returns choices and excludes the previous deck", async () => {
  const bodies = [];
  const { app, document } = consoleApp(async (url, opts) => {
    bodies.push(JSON.parse(opts.body));
    return { ok: true, json: async () => ({ items: ["ZBrush：DynaMesh", "ZBrush：ZRemesher"] }) };
  });
  document.querySelector("#direction-seed").value = "ZBrush";
  await app.refreshDirections();
  await app.refreshDirections();
  assert.deepEqual(bodies[0].exclude, []);
  assert.deepEqual(bodies[1].exclude, ["ZBrush：DynaMesh", "ZBrush：ZRemesher"]);
  assert.match(document.querySelector("#direction-options").innerHTML, /DynaMesh/);
});

test("changing the seed during a refresh discards stale choices", async () => {
  let resolve;
  const { app, document } = consoleApp(() => new Promise((r) => { resolve = r; }));
  document.querySelector("#direction-seed").value = "ZBrush";
  const pending = app.refreshDirections();
  document.querySelector("#direction-seed").value = "Blender";
  resolve(response(["ZBrush：DynaMesh"]));
  await pending;
  assert.equal(document.querySelector("#direction-options").innerHTML, "");
});

test("failed learning admission retains the activity stream and unlocks start", async () => {
  const { app, document } = consoleApp(async () => ({ ok: true, json: async () => ({ ok: false, msg: "busy" }) }));
  document.querySelector("#direction").value = "ZBrush";
  document.querySelector("#rounds").value = "3";
  document.querySelector("#learn-stream").innerHTML = "previous activity";
  await app.startLearning();
  assert.equal(document.querySelector("#learn-stream").innerHTML, "previous activity");
  assert.equal(document.querySelector("#btn-start").disabled, false);
});

test("rapid learning clicks submit only one request", async () => {
  let count = 0, resolve;
  const { app, document } = consoleApp(() => { count++; return new Promise((r) => { resolve = r; }); });
  document.querySelector("#rounds").value = "3";
  const first = app.startLearning("ZBrush：DynaMesh");
  await app.startLearning("ZBrush：ZRemesher");
  resolve({ ok: true, json: async () => ({ ok: true }) });
  await first;
  await app.startLearning("ZBrush：ZRemesher");
  assert.equal(count, 1);
  assert.equal(document.querySelector("#btn-start").disabled, true);
});

test("refinement rendering escapes content and preserves expanded history on polling", async () => {
  const row = { topic: "<topic>", input_count: 5, before_chars: 100, after_chars: 30, created_at: 1,
    items: [{ claim: "<script>bad</script>", detail: "short", source_ids: ["p1"] }],
    sources: [{ id: "p1", claim: "original", source_url: "https://example.org" }] };
  const { app, document } = consoleApp(async () => ({ ok: true, json: async () => ({ items: [row], state: { running: false } }) }));
  await app.loadRefinements();
  const el = document.querySelector("#refine-results");
  assert.match(el.innerHTML, /&lt;script&gt;/);
  assert.match(el.innerHTML, /data-pid="p1"/);
  assert.match(el.innerHTML, /来源原文/);
  const writes = el.writes;
  await app.loadRefinements();
  assert.equal(el.writes, writes);
});

test("continuous learning displays seconds and the configured round count", () => {
  const { app, document } = consoleApp(async () => response());
  app.paintAuto({ enabled: true, direction: "ZBrush", idle_seconds: 30, auto_rounds: 7, runs: 2 });
  const text = document.querySelector("#auto-desc").innerHTML;
  assert.match(text, /30 秒/);
  assert.match(text, /7 轮/);
  assert.doesNotMatch(text, /分钟/);
});

test("topic choices use all topics even when the current page only contains one", async () => {
  const { app, document } = consoleApp(async () => ({ ok: true, json: async () => ({
    items: [{ claim: "new", topic: "new topic", created_at: 1 }],
    topics: [{ topic: "old topic" }, { topic: "new topic" }], total: 2,
  }) }));
  document.querySelector("#kb-topic").value = "old topic";
  await app.loadKnowledge();
  assert.match(document.querySelector("#kb-topic").innerHTML, /old topic/);
  assert.match(document.querySelector("#kb-topic").innerHTML, /new topic/);
  assert.equal(document.querySelector("#kb-topic").value, "old topic");
});

test("polling retains the submitted search and topic, and clearing resets the query", async () => {
  const urls = [];
  const { app, document } = consoleApp(async (url) => { urls.push(url); return response(); });
  document.querySelector("#kb-topic").value = "topic with spaces";
  await app.loadKnowledge("vector search");
  await app.loadKnowledge();
  // 只核对知识库列表请求：同一轮还会拉一次 /api/knowledge/duplicates（近重复卡片）
  for (const url of urls.filter((u) => u.startsWith("/api/knowledge?"))) {
    const params = new URL(url, "http://localhost").searchParams;
    assert.equal(params.get("q"), "vector search");
    assert.equal(params.get("topic"), "topic with spaces");
  }
  await app.loadKnowledge("");
  const last = urls.filter((u) => u.startsWith("/api/knowledge?")).at(-1);
  assert.equal(new URL(last, "http://localhost").searchParams.has("q"), false);
});

test("a late search response cannot replace the newer results", async () => {
  const pending = [];
  const { app, document } = consoleApp(() => new Promise((resolve) => pending.push(resolve)));
  const old = app.loadKnowledge("old");
  const newer = app.loadKnowledge("new");
  pending[1](response([{ claim: "new result", topic: "test", created_at: 1 }]));
  await newer;
  pending[0](response([{ claim: "stale result", topic: "test", created_at: 1 }]));
  await old;
  const html = document.querySelector("#kb-list").innerHTML;
  assert.match(html, /new result/);
  assert.doesNotMatch(html, /stale result/);
});

test("slow status requests do not overlap and hidden tabs skip polling", async () => {
  let requests = 0;
  let finish;
  const { app, document } = consoleApp((url) => {
    // 只有 /api/status 挂住；其余端点（建议/健康度等）立即返回，
    // 否则 refreshAll 会在建议请求上永久挂起，拖垮整个测试文件
    if (url.includes("/api/status")) {
      requests++;
      return new Promise((resolve) => { finish = resolve; });
    }
    return { ok: true, json: async () => ({ items: [] }) };
  });
  const first = app.refreshAll();
  await app.refreshAll();
  assert.equal(requests, 1);
  finish({ ok: true, json: async () => ({
    stats: { points: 0, documents: 0, disk_gb: 0 }, engine: { running: false },
    llm: { healthy: false }, finetune: { cuda: false, ready: false },
  }) });
  await first;
  document.hidden = true;
  await app.refreshAll();
  assert.equal(requests, 1);
});

test("polling recovers after an HTTP failure", async () => {
  let requests = 0;
  const { app } = consoleApp(async () => { requests++; throw new Error("offline"); });
  await app.refreshAll();
  await app.refreshAll();
  assert.equal(requests, 2);
});

/* ---- 第十一轮：写入版本号门控 ---- */
const analyticsPayload = (ver) => ({
  ver,
  total: 10,
  growth: [{ day: "2026-10-01", added: 2, cumulative: 2 }],
  topics: [{ topic: "方向甲", count: 2, avg_conf: 0.6 }],
  confidence: [{ lo: 0.5, hi: 0.6, n: 2 }],
  sources: [{ kind: "web", count: 2 }],
  graph: { nodes: [], edges: [] },
  sessions: { sessions: [], total_sessions: 0, total_points: 0, total_rounds: 0 },
});

test("analytics polling skips chart rebuild when the write version is unchanged", async () => {
  const payload = analyticsPayload(5);
  const { app, document } = consoleApp(async (url) => ({
    ok: true,
    json: async () =>
      url.includes("/api/analytics")
        ? payload
        : url.includes("/api/perf")
          ? { search_cache_hit_rate: 0.5, search_avg_ms: 1, search_calls: 2, status_avg_ms: 1 }
          : {},
  }));
  const growth = document.querySelector("#an-growth");
  await app.loadAnalytics(true);
  const afterFirst = growth.writes;
  assert.ok(afterFirst >= 1, "首次应渲染增长图");
  await app.loadAnalytics(); // 同版本：跳过整轮图表重建
  assert.equal(growth.writes, afterFirst, "版本未变不应重建图表");
  payload.ver = 6; // 库写过：必须重绘（数据同时变，否则 setHTML 的内容 diff 也会跳过）
  payload.growth = [{ day: "2026-10-02", added: 5, cumulative: 7 }];
  await app.loadAnalytics();
  assert.ok(growth.writes > afterFirst, "版本变化应重绘图表");
});

test("knowledge polling skips refetch when the write version is unchanged", async () => {
  const urls = [];
  const { app } = consoleApp(async (url) => {
    urls.push(url);
    if (url.includes("/api/status")) {
      return {
        ok: true,
        json: async () => ({
          stats: { points: 1, documents: 0, disk_gb: 0, ver: 7 },
          engine: { running: false, stage: "idle", round: 0 },
          llm: { healthy: true },
          finetune: { ready: false, cuda: false },
          config: {}, disk: {},
        }),
      };
    }
    if (url.includes("/api/knowledge")) {
      return { ok: true, json: async () => ({ items: [{ claim: "一条知识", topic: "方向甲", created_at: 1 }] }) };
    }
    return { ok: true, json: async () => ({ items: [] }) };
  });
  app.state.view = "knowledge";
  await app.refreshAll();
  await app.refreshAll();
  await app.refreshAll();
  const kbFetches = urls.filter((u) => u.startsWith("/api/knowledge?"));
  assert.equal(kbFetches.length, 1, "版本未变时知识库只应拉取一次");
  // 近重复扫描同样按版本门控，不该每轮重问
  const dupFetches = urls.filter((u) => u.startsWith("/api/knowledge/duplicates"));
  assert.ok(dupFetches.length <= 3, `近重复请求次数异常：${dupFetches.length}`);
});

/* ---- 第十四轮：成长里程碑进度条 ---- */
test("milestone bar shows current progress and the next unlock target", () => {
  const { app, document } = consoleApp(async () => ({ ok: true, json: async () => ({}) }));
  app.renderMilestones(45); // 已过 30，未到 120
  const html = document.querySelector("#milestone").innerHTML;
  assert.match(html, /检验所学/);          // 30 里程碑已达成，应出现
  assert.match(html, /权重进化/);          // 下一个目标里程碑
  assert.match(html, /再学\s*<b>75<\/b>\s*条/); // 120 - 45 = 75
  assert.match(html, /data-goto="finetune"/); // 点里程碑可跳转

  app.renderMilestones(0);
  const empty = document.querySelector("#milestone").innerHTML;
  assert.match(empty, /还没有知识/);

  app.renderMilestones(2000);
  const full = document.querySelector("#milestone").innerHTML;
  assert.match(full, /已抵达全部里程碑/);
});

/* ---- 第十五轮：活动流改 SSE 实时推送，去冗余轮询 ---- */
const statusPayload = (ver = 7) => ({
  stats: {
    points: 12, documents: 1, disk_gb: 0, new_24h: 2, sessions: 1,
    resources: 0, scale_warn: null, topics: [], ver,
  },
  engine: { running: false, stage: "idle", round: 0 },
  llm: { healthy: true }, finetune: { ready: false, cuda: false },
  config: { auto_download: true }, disk: {}, response_ms: 0.3,
});

function feedConsole(recentEvents) {
  const urls = [];
  const { app, document, es } = consoleApp(async (url) => {
    urls.push(url);
    if (url.includes("/api/status")) return { ok: true, json: async () => statusPayload() };
    if (url.includes("/api/events/recent"))
      return { ok: true, json: async () => recentEvents };
    if (url.includes("/api/analytics/health")) return { ok: true, json: async () => ({ empty: true }) };
    return { ok: true, json: async () => ({ items: [] }) };
  });
  app.state.view = "dashboard";
  return { app, document, es, urls };
}

test("dashboard activity feed is pushed live over SSE, not polled every cycle", async () => {
  const recent = [
    { id: 1, level: "info", msg: "历史一", ts: 1 },
    { id: 2, level: "ok", msg: "历史二", ts: 2 },
  ];
  const { app, document, es, urls } = feedConsole(recent);
  // 初次进总览：拉一次 recent 打底（之后交给 SSE）
  await app.loadDashboard(null, true);
  const afterInit = urls.filter((u) => u.includes("/api/events/recent")).length;
  assert.equal(afterInit, 1, "初次应拉一次 recent 打底");
  const box = document.querySelector("#dash-stream");
  assert.match(box.innerHTML, /历史一/);
  assert.match(box.innerHTML, /历史二/);

  // SSE 上线：徽标变「实时」
  es.onopen();
  assert.equal(document.querySelector("#feed-live").textContent, "实时");

  // SSE 实时推三条新事件，立即进总览活动流
  for (const id of [3, 4, 5])
    es.onmessage({ data: JSON.stringify({ id, level: "info", msg: `实时${id}`, ts: 10 + id }) });
  assert.match(box.innerHTML, /实时3/);
  assert.match(box.innerHTML, /实时5/);

  // 之后每 6 秒的 refreshAll 不再轮询 /api/events/recent（SSE 已实时推送）
  await app.refreshAll();
  await app.refreshAll();
  await app.refreshAll();
  assert.equal(
    urls.filter((u) => u.includes("/api/events/recent")).length,
    afterInit,
    "SSE 在线时不应再轮询 /api/events/recent"
  );
});

test("SSE drop falls back to polling; reconnect replay never duplicates the feed", async () => {
  const recent = [{ id: 1, level: "info", msg: "事件一", ts: 1 }];
  const { app, document, es, urls } = feedConsole(recent);
  await app.loadDashboard(null, true);
  es.onopen();

  // 断开：徽标回退，活动流改轮询兜底（不僵死）
  es.onerror();
  assert.equal(document.querySelector("#feed-live").textContent, "重连中");
  await app.refreshAll();
  assert.ok(
    urls.filter((u) => u.includes("/api/events/recent")).length >= 2,
    "SSE 断开应回退轮询 recent"
  );

  // 重连：SSE 重放同一条历史，总览活动流不能重复堆叠
  es.onopen();
  es.onmessage({ data: JSON.stringify({ id: 1, level: "info", msg: "事件一", ts: 1 }) });
  const shown = (document.querySelector("#dash-stream").innerHTML.match(/事件一/g) || []).length;
  assert.equal(shown, 1, "重放历史不应在总览活动流里重复");
});

test("书库目录：完成度进度条、三种状态胶囊、总览引导", () => {
  const { app, document } = consoleApp(async () => ({ ok: true, json: async () => [] }));
  const dirs = [
    { id: "d0", path: "/a", label: "全部导入", book_count: 10, imported_count: 10, excludes: [] },
    { id: "d1", path: "/b", label: "还差三本", book_count: 10, imported_count: 7, excludes: [] },
    { id: "d2", path: "/c", label: "空目录", book_count: 0, imported_count: 0, excludes: ["*.tmp"] },
  ];
  app.renderLibDirs(dirs);

  const html = document.querySelector("#lib-dir-list").innerHTML;
  // 进度宽度：100% / 70% / 0%
  assert.match(html, /width:100%/, "已全部导入应为满进度");
  assert.match(html, /width:70%/, "7/10 应为 70%");
  assert.match(html, /width:0%/, "空目录应为 0%");
  // 三种状态胶囊
  assert.match(html, /已全部导入/);
  assert.match(html, /待导入 3 本/);
  assert.match(html, /未扫描/);
  // 满进度用绿色，其余用琥珀
  assert.match(html, /class="full"/);
  // 排除规则仍然渲染出来
  assert.match(html, /排除 \*\.tmp/);

  // 总览：数量 + 总进度 + 引导（有未扫描目录时优先提示去扫描）
  // 第十七轮起百分比单独成一列（dirsum-pct），便于右对齐，所以分两处断言
  const sum = document.querySelector("#lib-dir-summary").innerHTML;
  assert.match(sum, /3 个目录 · 20 本 · 已导入 17 本/);
  assert.match(sum, /dirsum-pct[^>]*>85%</);
  assert.match(sum, /width:85%/);
  assert.match(sum, /1 个目录还没扫描过/);
});

test("书库目录：下钻选中态与范围失效保护", () => {
  const { app, document } = consoleApp(async () => ({ ok: true, json: async () => [] }));
  const dirs = [
    { id: "d0", path: "/a", label: "甲", book_count: 2, imported_count: 1, excludes: [] },
    { id: "d1", path: "/b", label: "乙", book_count: 2, imported_count: 2, excludes: [] },
  ];
  app.state.libDir = "d1";
  app.renderLibDirs(dirs);
  assert.match(
    document.querySelector("#lib-dir-list").innerHTML,
    /lib-dir-row active[^>]*data-dir-open="d1"/,
    "选中目录应有 active 标记"
  );
  assert.deepEqual(app.state.libDirs.map((d) => d.id), ["d0", "d1"]);

  // 选中的目录被删掉后，下钻范围必须自动失效，否则会筛出一个空列表
  app.renderLibDirs([dirs[0]]);
  assert.equal(app.state.libDir, null, "选中目录消失后应清空下钻范围");
});

test("书库目录：无目录时给出添加引导而不是空白", () => {
  const { app, document } = consoleApp(async () => ({ ok: true, json: async () => [] }));
  app.renderLibDirs([]);
  assert.match(document.querySelector("#lib-dir-summary").innerHTML, /添加一个书库目录/);
  assert.match(document.querySelector("#lib-dir-list").innerHTML, /还没有添加目录/);
});

test("轮换面板：显示当前方向、剩余轮次与子方向状态", () => {
  const { app, document } = consoleApp(async () => response());
  app.paintRotation({
    active: true, paused: false, stage: "learning", seed: "光固化 3D 打印",
    current: "光固化 3D 打印：收缩补偿模型", rounds_in_current: 40,
    rounds_per_direction: 100, total_rounds_done: 40, total_round_limit: 300,
    direction_index: 1, direction_total: 6, progress: 0.1333,
    plan: [
      { direction: "光固化 3D 打印：收缩补偿模型", status: "active", rounds: 40, points: 12 },
      { direction: "光固化 3D 打印：层厚与曝光", status: "pending", rounds: 0, points: 0 },
      { direction: "Blender 拓扑重网格", status: "rejected", rounds: 0, points: 0, note: "越界" },
    ],
  });
  const status = document.querySelector("#rot-status").innerHTML;
  assert.match(status, /还剩 60 轮/);
  assert.match(status, /40\/300/);
  assert.match(status, /子方向 1\/6/);
  assert.equal(document.querySelector("#btn-rot-start").disabled, true);
  assert.equal(document.querySelector("#btn-rot-pause").disabled, false);
  assert.equal(document.querySelector("#btn-rot-resume").disabled, true);
  assert.equal(document.querySelector("#btn-rot-stop").disabled, false);
  assert.equal(document.querySelector("#rot-bar").hidden, false);
  assert.match(document.querySelector("#rot-plan-list").innerHTML, /rot-item active/);
  assert.match(document.querySelector("#rot-plan-list").innerHTML, /越界已丢弃/);
});

test("轮换面板：暂停态与结束后不再可暂停", () => {
  const { app, document } = consoleApp(async () => response());
  app.paintRotation({
    active: true, paused: true, stage: "paused", seed: "量子计算",
    current: "量子计算：表面码纠错", rounds_in_current: 20, rounds_per_direction: 100,
    total_rounds_done: 120, total_round_limit: 300, plan: [],
  });
  assert.equal(document.querySelector("#rot-tag").textContent, "已暂停");
  assert.equal(document.querySelector("#btn-rot-pause").disabled, true);
  assert.equal(document.querySelector("#btn-rot-resume").disabled, false);

  app.paintRotation({
    active: false, paused: false, stage: "done", seed: "量子计算",
    total_rounds_done: 300, total_round_limit: 300, stop_reason: "total_rounds", plan: [],
  });
  assert.equal(document.querySelector("#rot-tag").textContent, "已完成");
  assert.equal(document.querySelector("#btn-rot-start").disabled, false);
  assert.equal(document.querySelector("#btn-rot-stop").disabled, true);
});

test("轮换启动：校验范围后按配置提交参数", async () => {
  const bodies = [];
  const { app, document } = consoleApp(async (url, opts) => {
    if (url === "/api/rotation/start") {
      bodies.push(JSON.parse(opts.body));
      return { ok: true, json: async () => ({ ok: true }) };
    }
    if (url === "/api/rotation") {
      return { ok: true, json: async () => ({ active: true, seed: "光固化 3D 打印", plan: [] }) };
    }
    return { ok: true, json: async () => ({}) };
  });
  document.querySelector("#rot-seed").value = "光固化 3D 打印";
  document.querySelector("#rot-per").value = "50";
  document.querySelector("#rot-total").value = "120";
  document.querySelector("#rot-count").value = "5";
  document.querySelector("#rot-window").value = "3";
  document.querySelector("#rot-boundary").value = "1";
  await document.querySelector("#btn-rot-start").listeners.click();
  assert.equal(bodies.length, 1);
  assert.deepEqual(bodies[0], {
    seed: "光固化 3D 打印", rounds_per_direction: 50, total_rounds: 120, subdirection_count: 5,
  });

  document.querySelector("#rot-count").value = "1";
  await document.querySelector("#btn-rot-start").listeners.click();
  assert.equal(bodies.length, 1, "子方向数量越界时不得提交");
});

test("轮换汇总：逐方向列出轮次与新增条数", () => {
  const { app, document } = consoleApp(async () => response());
  app.renderRotationSummary({
    seed: "光固化 3D 打印", total_rounds: 300, total_limit: 300, rounds_per_direction: 100,
    points_total: 88, switch_count: 2,
    switches: [{ from: "", to: "收缩补偿模型" }, { from: "收缩补偿模型", to: "层厚与曝光" }],
    directions: [
      { direction: "收缩补偿模型", rounds: 100, points: 40, highlights: ["收缩率随层厚变化"] },
      { direction: "层厚与曝光", rounds: 100, points: 30, highlights: [] },
    ],
  });
  const html = document.querySelector("#rot-summary").innerHTML;
  assert.match(html, /收缩补偿模型：100 轮 \/ 40 条/);
  assert.match(html, /切换 2 次/);
  assert.match(html, /收缩率随层厚变化/);
  assert.equal(document.querySelector("#rot-summary").hidden, false);

  app.renderRotationSummary({});
  assert.equal(document.querySelector("#rot-summary").hidden, true);
});

test("轮换历史：列出最近任务的方向与轮次", () => {
  const { app, document } = consoleApp(async () => response());
  app.paintRotationHistory([
    { seed: "光固化 3D 打印", status: "done", status_text: "已完成",
      total_rounds: 300, total_limit: 300, started_at: 1700000000 },
    { seed: "RAG 检索增强", status: "stopped", status_text: "已手动结束",
      total_rounds: 40, total_limit: 200, started_at: 1700003600 },
  ]);
  const html = document.querySelector("#rot-history-list").innerHTML;
  assert.match(html, /光固化 3D 打印/);
  assert.match(html, /300\/300 轮 · 已完成/);
  assert.match(html, /40\/200 轮 · 已手动结束/);
  assert.equal(document.querySelector("#rot-history").hidden, false);

  app.paintRotationHistory([]);
  assert.equal(document.querySelector("#rot-history").hidden, true);
});

test("轮换汇总：可一键复制成文本", async () => {
  const { app, document, copied } = consoleApp(async () => response());
  app.renderRotationSummary({
    seed: "光固化 3D 打印", total_rounds: 20, total_limit: 300, rounds_per_direction: 10,
    points_total: 7, switch_count: 1, switches: [],
    directions: [{ direction: "收缩补偿模型", rounds: 10, points: 7, highlights: ["收缩率随层厚变化"] }],
  });
  assert.equal(document.querySelector("#rot-summary-actions").hidden, false);
  await document.querySelector("#btn-rot-copy").listeners.click();
  assert.equal(copied.length, 1);
  assert.match(copied[0], /方向轮换汇总/);
  assert.match(copied[0], /收缩补偿模型/);
});

test("轮换面板：进度条与禁用按钮有可读的无障碍信息", () => {
  const { app, document } = consoleApp(async () => response());
  app.paintRotation({
    active: false, paused: false, seed: "量子计算", total_rounds_done: 40,
    total_round_limit: 300, plan: [],
  });
  const bar = document.querySelector("#rot-bar");
  assert.equal(bar.getAttribute("role"), "progressbar");
  assert.equal(bar.getAttribute("aria-valuemax"), "300");
  assert.equal(bar.getAttribute("aria-valuenow"), "40");
  assert.match(document.querySelector("#btn-rot-pause").title, /没有运行中的任务/);
  assert.match(document.querySelector("#btn-rot-start").title, /开始自动轮换/);
});

test("轮换汇总：剪贴板不可用时给出替代提示", async () => {
  const { app, document, copied } = consoleApp(async () => response());
  copied.fail = true;
  app.renderRotationSummary({
    seed: "量子计算", total_rounds: 5, total_limit: 10, rounds_per_direction: 5,
    points_total: 2, switch_count: 0, switches: [],
    directions: [{ direction: "表面码纠错", rounds: 5, points: 2, highlights: [] }],
  });
  await document.querySelector("#btn-rot-copy").listeners.click();
  const toasts = document.querySelector("#toasts").children;
  assert.match(toasts[toasts.length - 1].textContent, /手动选中/);
});

test("轮换面板：重启后回退显示上一次任务", async () => {
  const { app, document } = consoleApp(async (url) => {
    if (url === "/api/rotation") {
      return { ok: true, json: async () => ({
        active: false, plan: [], stop_reason: "",
        last_run: {
          seed: "RAG 检索增强", total_rounds: 40, total_limit: 200,
          directions: [{ direction: "分块策略与重叠窗口", rounds: 40, points: 5, status: "stopped" }],
        },
      }) };
    }
    return { ok: true, json: async () => ({}) };
  });
  await app.loadRotation();
  assert.match(document.querySelector("#rot-status").innerHTML, /上次任务/);
  assert.match(document.querySelector("#rot-status").innerHTML, /40\/200 轮/);
  assert.match(document.querySelector("#rot-plan-list").innerHTML, /分块策略与重叠窗口/);
});

test("轮换面板：状态轮询不会刷掉最近任务", () => {
  const { app, document } = consoleApp(async () => response());
  app.paintRotation({ active: true, plan: [], history: [
    { seed: "RAG 检索增强", status: "done", status_text: "已完成",
      total_rounds: 300, total_limit: 300, started_at: 1700000000 },
  ] });
  assert.equal(document.querySelector("#rot-history").hidden, false);
  // /api/status 的 rotation 不带 history：不能因此清空
  app.paintRotation({ active: true, plan: [], seed: "RAG 检索增强" });
  assert.equal(document.querySelector("#rot-history").hidden, false);
  assert.match(document.querySelector("#rot-history-list").innerHTML, /RAG 检索增强/);
});

test("轮换面板：可跳过当前方向，未选中时禁用", async () => {
  const calls = [];
  const { app, document } = consoleApp(async (url) => {
    calls.push(url);
    if (url === "/api/rotation") {
      return { ok: true, json: async () => ({ active: true, current: "A", plan: [] }) };
    }
    return { ok: true, json: async () => ({ ok: true }) };
  });
  app.paintRotation({ active: true, paused: false, seed: "x", current: "光固化：A",
    total_rounds_done: 3, total_round_limit: 10, plan: [] });
  const skip = document.querySelector("#btn-rot-skip");
  assert.equal(skip.disabled, false);
  await skip.listeners.click();
  assert.ok(calls.includes("/api/rotation/skip"), "点击应提交跳过请求");

  app.paintRotation({ active: true, paused: false, seed: "x", current: "", plan: [] });
  assert.equal(document.querySelector("#btn-rot-skip").disabled, true);
});

test("轮换：预览过的子方向随启动提交，换母方向后不再沿用", async () => {
  const bodies = [];
  const { app, document } = consoleApp(async (url, opts) => {
    if (url === "/api/rotation/plan") {
      return { ok: true, json: async () => ({ seed: "光固化 3D 打印", items: ["A 方向", "B 方向"] }) };
    }
    if (url === "/api/rotation/start") {
      bodies.push(JSON.parse(opts.body));
      return { ok: true, json: async () => ({ ok: true }) };
    }
    if (url === "/api/rotation") return { ok: true, json: async () => ({ active: true, plan: [] }) };
    return { ok: true, json: async () => ({}) };
  });
  document.querySelector("#rot-seed").value = "光固化 3D 打印";
  document.querySelector("#rot-count").value = "2";
  document.querySelector("#rot-per").value = "5";
  document.querySelector("#rot-total").value = "10";
  document.querySelector("#rot-window").value = "3";
  await document.querySelector("#btn-rot-plan").listeners.click();
  await document.querySelector("#btn-rot-start").listeners.click();
  assert.deepEqual(bodies[0].items, ["A 方向", "B 方向"], "同一次预览的方向应随启动提交");

  document.querySelector("#rot-seed").value = "量子计算";
  await document.querySelector("#btn-rot-start").listeners.click();
  assert.equal(bodies[1].items, undefined, "换了母方向不能沿用旧预览");
});

test("轮换面板：非「自主进化」页不重建子方向列表", async () => {
  const { app, document } = consoleApp(async (url) => {
    if (url === "/api/status") {
      return { ok: true, json: async () => ({
        revision: "x", capabilities: [], config: {}, disk: {}, stats: {},
        engine: { running: false, round: 0, total_rounds: 0, stage: "idle" },
        rotation: { active: true, seed: "光固化 3D 打印", current: "A",
          plan: [{ direction: "A", status: "active", rounds: 1, points: 1 }] },
        refinement: { running: false }, auto: { enabled: false },
        llm: { healthy: true, models: [], checking: false },
        finetune: { ready: false }, finetune_state: {},
      }) };
    }
    return { ok: true, json: async () => ({ items: [], total: 0 }) };
  });
  app.state.view = "dashboard";
  await app.refreshAll();
  const list = document.querySelector("#rot-plan-list");
  const before = list.writes;
  await app.refreshAll();
  assert.equal(list.writes, before, "不可见的面板不该被重画");

  app.state.view = "learn";
  await app.refreshAll();
  assert.ok(list.writes > before, "切到「自主进化」页应正常渲染");
});

test("轮换面板：模型不可用时显示等待与重试倒计时", () => {
  const { app, document } = consoleApp(async () => response());
  app.paintRotation({
    active: true, paused: false, stage: "waiting_model",
    seed: "光固化 3D 打印", current: "光固化 3D 打印：A", rounds_per_direction: 100,
    total_rounds_done: 40, total_round_limit: 300, retry_in: 120, plan: [],
  });
  assert.equal(document.querySelector("#rot-tag").textContent, "等待模型");
  const status = document.querySelector("#rot-status").innerText;
  assert.match(status, /等待模型/);
  assert.match(status, /120 秒后自动重试/);
  // 等待不是暂停：暂停按钮仍可用，恢复按钮仍禁用
  assert.equal(document.querySelector("#btn-rot-pause").disabled, false);
  assert.equal(document.querySelector("#btn-rot-resume").disabled, true);
});


test("高相似度清理：先完整预览，确认后提交阈值、范围和快照", async () => {
  const writes = [];
  const point = {id:"a",topic:"CAD",claim:"<script>example</script>",detail:"完整内容",created_at:1};
  const preview = {threshold:.9,topic:"CAD",token:"snapshot",removed:1,groups:1,items:[{keep:point,drop:{...point,id:"b"},sim:.96,reason:"更完整"}]};
  const {app,document} = consoleApp(async (url, opts) => {
    if (opts?.method === "POST") { writes.push({url,body:JSON.parse(opts.body)}); return {ok:true,json:async()=>({ok:true,removed:1,undo:{available:true}})}; }
    if (url.startsWith("/api/knowledge/cleanup/preview")) return {ok:true,json:async()=>preview};
    return response();
  });
  document.querySelector("#dup-threshold").value = "90";
  document.querySelector("#kb-topic").value = "CAD";
  await app.previewSimilarKnowledge();
  assert.equal(writes.length,0,"预览不可删除知识点");
  assert.match(document.querySelector("#dup-preview-list").innerHTML,/96.0%/);
  assert.match(document.querySelector("#dup-preview-list").innerHTML,/&lt;script&gt;/);
  assert.match(document.querySelector("#dup-preview-summary").textContent,/待清除 1 条/);
  await app.confirmSimilarCleanup();
  assert.equal(writes.length,1);
  assert.deepEqual(writes[0].body,{threshold:.9,topic:"CAD",token:"snapshot"});
  assert.equal(document.querySelector("#dup-preview").hidden,true);
  assert.equal(document.querySelector("#btn-cleanup-undo").disabled,false);
});

test("高相似度清理：取消和阈值变化使预览失效，晚到的响应不会覆盖", async () => {
  let resolvePreview;
  let writes = 0;
  const {app,document} = consoleApp(async (url,opts) => {
    if(opts?.method === "POST") writes++;
    if(url.startsWith("/api/knowledge/cleanup/preview")) return new Promise(resolve => {resolvePreview=resolve;});
    return response();
  });
  const pending = app.previewSimilarKnowledge();
  document.querySelector("#dup-threshold").value="99";
  document.querySelector("#dup-threshold").listeners.input();
  resolvePreview({ok:true,json:async()=>({threshold:.9,token:"old",removed:1,groups:1,items:[]})});
  await pending;
  await app.confirmSimilarCleanup();
  assert.equal(writes,0);
  assert.equal(document.querySelector("#dup-preview").hidden,true);
  assert.equal(document.querySelector("#btn-confirm-similar").disabled,true);
});
