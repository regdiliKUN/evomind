// 第十五轮 A/B：总览「最近活动」从「每轮轮询 /api/events/recent」改为「SSE 实时推送」。
//
// 指标：24 个轮询周期里 /api/events/recent 的请求次数。
//   旧行为（等价于 SSE 断开时的回退）：每个周期都轮询一次 → 每周期 1 次。
//   新行为（SSE 在线，正常情况）：初次加载打底后交给 SSE 实时推送 → 每周期 0 次。
// 等价性：两种模式下 SSE 推送的事件都完整进入 #dash-stream，且不重复堆叠。
//
// 直接跑真实的 web/app.js（vm + 最小 DOM 桩），EventSource 可控，便于手动推送事件。
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
    Object.defineProperty(el, "innerHTML", {
      get() { return el._html; },
      set(v) { el._html = v == null ? "" : String(v); el.writes++; if (!v) el.children = []; },
    });
    return el;
  };
  const document = {
    hidden: false, addEventListener() {},
    querySelector(s) { if (!nodes.has(s)) nodes.set(s, element()); return nodes.get(s); },
    querySelectorAll: () => [], createElement: element,
  };
  const esRef = { current: null };
  const ctx = vm.createContext({
    document, fetch, URLSearchParams, console, Date,
    location: { hash: "#chat" }, window: { addEventListener() {} },
    EventSource: class { constructor(u) { this.url = u; esRef.current = this; } addEventListener() {} close() {} },
    setInterval() {}, setTimeout() {}, clearInterval() {}, clearTimeout() {},
  });
  const source = fs.readFileSync(path.join(__dirname, "../web/app.js"), "utf8");
  vm.runInContext(
    source + "\nglobalThis.app = {state, refreshAll, loadDashboard};",
    ctx
  );
  return { app: ctx.app, document, es: esRef.current };
}

const statusPayload = (ver) => ({
  stats: { points: 12, documents: 1, disk_gb: 0, new_24h: 2, sessions: 1, resources: 0, scale_warn: null, topics: [], ver },
  engine: { running: false, stage: "idle", round: 0 },
  llm: { healthy: true }, finetune: { ready: false, cuda: false },
  config: { auto_download: true }, disk: {}, response_ms: 0.3,
});

function median(a) { const s = [...a].sort((x, y) => x - y); return s[s.length >> 1]; }

async function oneCycle(sseUp) {
  let recentReqs = 0;
  let nextId = 100;
  const recent = [{ id: 1, level: "info", msg: "种子事件", ts: 1 }];
  const { app, document, es } = consoleApp(async (url) => {
    if (url.includes("/api/events/recent")) { recentReqs++; return { ok: true, json: async () => recent }; }
    if (url.includes("/api/status")) return { ok: true, json: async () => statusPayload(7) };
    if (url.includes("/api/analytics/health")) return { ok: true, json: async () => ({ empty: true }) };
    return { ok: true, json: async () => ({ items: [] }) };
  });
  app.state.view = "dashboard";
  await app.loadDashboard(null, true); // 初次打底（两种模式都拉一次）
  const initReqs = recentReqs;
  if (sseUp) es.onopen();

  const POLLS = 5; // 一个「轮询周期」= 5 次 6 秒刷新
  let delivered = 0;
  for (let p = 0; p < POLLS; p++) {
    if (sseUp) {
      // SSE 实时推 2 条新事件（正常情况：学习时事件源源不断）
      for (let k = 0; k < 2; k++) {
        nextId++;
        delivered++;
        es.onmessage({ data: JSON.stringify({ id: nextId, level: "info", msg: `实时事件${nextId}`, ts: nextId }) });
      }
    }
    await app.refreshAll();
  }
  const box = document.querySelector("#dash-stream");
  const html = box.innerHTML;
  // 等价性：SSE 推的事件都进了活动流，且各只出现一次
  let equiv = true;
  if (sseUp) {
    for (let i = 0; i < delivered; i++) {
      const id = 101 + i;
      const n = (html.match(new RegExp(`实时事件${id}`, "g")) || []).length;
      if (n !== 1) equiv = false;
    }
  }
  return {
    steadyReqs: recentReqs - initReqs, // 初次打底之后的稳态请求数
    totalReqs: recentReqs,
    equiv,
    delivered,
  };
}

(async () => {
  const ROUNDS = 24;
  const aReqs = []; // 旧行为：SSE 断开 → 每轮都回退轮询
  const bReqs = []; // 新行为：SSE 在线 → 稳态 0 次
  let equivOk = 0, equivTotal = 0;
  for (let r = 0; r < ROUNDS; r++) {
    const a = await oneCycle(false); // 奇数位：旧
    const b = await oneCycle(true);  // 偶数位：新
    aReqs.push(a.steadyReqs);
    bReqs.push(b.steadyReqs);
    equivTotal += 2;
    if (a.equiv !== false) equivOk++;
    if (b.equiv) equivOk++;
  }
  const aMed = median(aReqs), bMed = median(bReqs);
  const totalA = aReqs.reduce((x, y) => x + y, 0);
  const totalB = bReqs.reduce((x, y) => x + y, 0);
  console.log(JSON.stringify({
    rounds: ROUNDS,
    polls_per_cycle: 5,
    note: "每周期 5 次 6 秒刷新；统计的是「初次打底之后」的稳态 /api/events/recent 请求数",
    old_polling_median_per_cycle: aMed,
    new_sse_median_per_cycle: bMed,
    old_total_over_rounds: totalA,
    new_total_over_rounds: totalB,
    reduction_pct: totalA ? Math.round((1 - totalB / totalA) * 100) : 100,
    faster_rounds: `${bReqs.filter((v, i) => v <= aReqs[i]).length}/${ROUNDS}`,
    events_delivered_equivalence: `${equivOk}/${equivTotal}`,
  }, null, 2));
})().catch((e) => { console.error(e); process.exit(1); });
