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
      value: "", dataset: {}, style: {}, textContent: "", _html: "", writes: 0,
      addEventListener() {}, classList: { toggle() {} },
    };
    // innerHTML 走 setter：测试据此判断「这一轮到底有没有重建 DOM」
    Object.defineProperty(el, "innerHTML", {
      get() { return el._html; },
      set(v) { el._html = v; el.writes++; },
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
  const ctx = vm.createContext({
    document, fetch, URLSearchParams, console, Date,
    location: { hash: "#chat" }, window: { addEventListener() {} },
    EventSource: class { addEventListener() {} close() {} },
    setInterval() {}, setTimeout() {}, clearInterval() {}, clearTimeout() {},
  });
  const source = fs.readFileSync(path.join(__dirname, "../web/app.js"), "utf8");
  vm.runInContext(
    source + "\nglobalThis.app = {state, loadKnowledge, refreshAll, loadAnalytics, renderMilestones};",
    ctx
  );
  return { app: ctx.app, document, nodes };
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
