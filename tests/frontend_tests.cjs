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
      addEventListener() {}, classList: { toggle() {} },
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
  const ctx = vm.createContext({
    document, fetch, URLSearchParams, console, Date,
    location: { hash: "#chat" }, window: { addEventListener() {} },
    EventSource: class {
      constructor(url) { this.url = url; esRef.current = this; }
      addEventListener() {} close() {}
    },
    setInterval() {}, setTimeout() {}, clearInterval() {}, clearTimeout() {},
  });
  const source = fs.readFileSync(path.join(__dirname, "../web/app.js"), "utf8");
  vm.runInContext(
    source + "\nglobalThis.app = {state, loadKnowledge, refreshAll, loadAnalytics, renderMilestones, loadDashboard, appendDashEvent, renderLibDirs, renderLibDirSummary};",
    ctx
  );
  return { app: ctx.app, document, nodes, es: esRef.current };
}

const response = (items = []) => ({ ok: true, json: async () => ({ items, total: items.length }) });

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
  const sum = document.querySelector("#lib-dir-summary").innerHTML;
  assert.match(sum, /3 个目录 · 20 本 · 已导入 17 本（85%）/);
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
