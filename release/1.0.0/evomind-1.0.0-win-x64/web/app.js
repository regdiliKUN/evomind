/* EvoMind 控制台 */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const state = {
  view: "dashboard",
  events: [],
  status: null,
  cfg: null,
  datasetPath: "",
  chatBusy: false,
  knowledgeQuery: "",
  knowledgeRequest: 0,
  suggestions: null,
  // 写入版本号（来自 /api/status 的 stats.ver）：库没变过就跳过整轮
  // 重新拉取 + 重渲染。知识库列表尤其需要——裸 innerHTML 重建会抹掉
  // 用户正在选的文字、把滚动位置弹回顶部。
  kbVer: -1,
  anVer: -1,
  dupVer: -1,
  pointId: null,
  // 书库按目录下钻：null 表示「全部目录」。目录卡上的进度条点一下就筛到该目录，
  // 让「哪个收藏夹还没导入」这件事一眼可见、一点可达。
  libDir: null,
};

/* ---------------- 工具 ---------------- */
function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transform = "translateY(8px)";
    el.style.transition = "all .3s";
    setTimeout(() => el.remove(), 320);
  }, 3600);
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try {
      const detail = (await r.json()).detail;
      if (typeof detail === "string") msg = detail;
      else if (Array.isArray(detail)) msg = detail.map((d) => d.msg).join("；");
    } catch (_) {}
    if (r.status === 404 && (path.startsWith("/api/refinements") || path.startsWith("/api/knowledge/cleanup"))) {
      msg = "当前后台服务尚未更新，请重启 EvoMind 后再试";
    }
    throw new Error(msg);
  }
  return r.json();
}

const fmtTime = (ts) => {
  const d = new Date(ts * 1000);
  return d.toTimeString().slice(0, 8);
};
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

/* ---------------- 导航 ---------------- */
const TITLES = {
  dashboard: "总览",
  learn: "自主进化",
  knowledge: "知识库",
  analytics: "分析",
  library: "书库",
  media: "多模态",
  resources: "资源获取",
  finetune: "权重进化",
  chat: "检验所学",
  settings: "设置",
};

function switchView(v, fromHash) {
  // 深链：#knowledge/<pid> 直接定位到某条知识点的详情（可复制分享）
  const [view, pid] = String(v || "").split("/");
  v = view;
  if (!TITLES[v]) v = "dashboard";
  state.view = v;
  if (!fromHash && location.hash.slice(1) !== v) location.hash = v;
  $$(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.view === v));
  $$(".view").forEach((s) => s.classList.toggle("active", s.id === "view-" + v));
  $("#page-title").textContent = TITLES[v] || v;
  if (v === "knowledge") { loadKnowledge(); loadRefinements(); loadCleanupState(); }
  if (v === "analytics") loadAnalytics();
  if (v === "library") loadLibrary();
  if (v === "media") loadMediaCaps();
  if (v === "resources") loadResources();
  if (v === "finetune") loadFinetune();
  if (v === "dashboard") { loadDashboard(null, true); refreshSuggestions(true); refreshHealth(true); }
  if (v === "settings") { fillSettings(); loadBackups(); }
  if (v === "learn") { loadAuto(); loadRotation(); }
  if (pid) openPoint(decodeURIComponent(pid));
}

$$(".nav-item").forEach((n) => n.addEventListener("click", () => switchView(n.dataset.view)));
window.addEventListener("hashchange", () => switchView(location.hash.slice(1), true));

/* 全局委托：所有 [data-goto] 跳转按钮（下一步建议 / 健康度 / 网络空态）——
   动态渲染的内容不必逐个重新绑定。 */
document.addEventListener("click", (e) => {
  const goto = e.target.closest("[data-goto]");
  if (goto) {
    // 带 data-topic 的跳转（如「在这个方向继续学」）：落地时顺手按该方向筛选
    const t = goto.dataset.topic || "";
    switchView(goto.dataset.goto);
    if (t && goto.dataset.goto === "knowledge") {
      selectTopic(t);
      loadKnowledge();
    }
    return;
  }
  // 分析页方向分布条 → 下钻知识库并按该方向筛选（跨视图联动）
  const drill = e.target.closest(".kb-drill");
  if (drill) {
    selectTopic(drill.dataset.topic || "");
    switchView("knowledge");
    loadKnowledge();
    return;
  }
  // 近重复整理：保留 A 就删掉 B（删单条是已有接口，用户明确点击才发生）
  const keep = e.target.closest(".dup-keep");
  if (keep) {
    const drop = keep.dataset.drop;
    if (!drop) return;
    if (!confirm(`删掉另一条相似的结论？\n\n${keep.dataset.claim || ""}\n\n删掉后不可恢复，但可以重新学习再入库。`)) return;
    api(`/api/knowledge/${encodeURIComponent(drop)}`, { method: "DELETE" })
      .then(() => {
        toast("已删除重复项");
        state.dupVer = -1;
        loadKnowledge();
      })
      .catch((err) => toast("删除失败：" + err.message, "err"));
    return;
  }
  // 知识点（列表行 / 详情里的相似条目）→ 打开详情抽屉；点来源链接不算
  const pt = e.target.closest("[data-pid]");
  if (pt && !e.target.closest("a")) {
    openPoint(pt.dataset.pid);
    return;
  }
  // 「就这条提问」：带着这条结论跳到问答，省得用户再打一遍
  const ask = e.target.closest(".pt-ask");
  if (ask) {
    closePoint();
    switchView("chat");
    const q = $("#chat-q");
    if (q) {
      q.value = `关于「${ask.dataset.claim || ""}」，这条结论成立的前提和边界是什么？`;
      q.focus();
    }
    return;
  }
  // 复制深链（#knowledge/<id>）：发给别人能直接打开同一条
  const copy = e.target.closest(".pt-copy");
  if (copy && state.pointId) {
    const url = `${location.origin}/#knowledge/${encodeURIComponent(state.pointId)}`;
    location.hash = `knowledge/${encodeURIComponent(state.pointId)}`;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(() => toast("链接已复制"), () => toast(url, "info"));
    } else {
      toast(url, "info");
    }
  }
});

/* 把某个方向塞进下拉并选中（下拉里没有就先补一个选项） */
function selectTopic(t) {
  const sel = $("#kb-topic");
  if (!sel) return;
  if (t && !Array.from(sel.options).some((o) => o.value === t)) {
    sel.insertAdjacentHTML("beforeend", `<option value="${esc(t)}">${esc(t)}</option>`);
  }
  sel.value = t || "";
}

/* 引导浮层：按钮接线 + 首次自动弹出 + 侧栏手动入口 */
$("#ob-next").addEventListener("click", () => {
  if (obStep >= OB_STEPS.length - 1) {
    closeOnboarding();
    return;
  }
  obStep += 1;
  renderOnboarding();
});
$("#ob-prev").addEventListener("click", () => {
  if (obStep > 0) {
    obStep -= 1;
    renderOnboarding();
  }
});
$("#ob-skip").addEventListener("click", closeOnboarding);
$("#btn-guide").addEventListener("click", openOnboarding);
$("#onboarding").addEventListener("click", (e) => {
  if (e.target === $("#onboarding")) closeOnboarding(); // 点遮罩关闭
});

/* ---------------- 事件流 ---------------- */
const STAGE_TEXT = {
  idle: "空闲", starting: "启动中", init: "初始化", planning: "诊断缺口",
  searching: "检索中", reading: "阅读中", extracting: "提炼中",
  resource: "资源扫描", reflecting: "反思中", done: "完成", error: "出错",
  stopping: "停止中",
};

function pushEvent(evt, streamEl, animate = true) {
  const box = streamEl || $("#learn-stream");
  // 空态占位有两种写法（.empty 与 .stream-empty），来第一条事件时都要清掉，
  // 否则「还没有开始」会一直挂在日志上方
  const empty = box.querySelector(".empty, .stream-empty");
  if (empty) empty.remove();

  const div = document.createElement("div");
  div.className = "evt " + (evt.level || "info") + (animate ? " enter" : "");
  const src = evt.source
    ? `<div class="src">↳ ${esc(String(evt.source).slice(0, 90))}</div>`
    : "";
  div.innerHTML = `<span class="t">${fmtTime(evt.ts || Date.now() / 1000)}</span>
    <span class="m">${esc(evt.msg)}${src}</span>`;
  box.appendChild(div);
  // 只有新事件才自动滚到底；重渲染历史时不动滚动位置，避免打断用户浏览
  if (animate) box.scrollTop = box.scrollHeight;

  while (box.children.length > 400) box.removeChild(box.firstChild);

  if (evt.level === "stage") {
    $("#stage-tag").textContent = evt.msg.includes("·")
      ? evt.msg.split("·")[1].trim()
      : evt.msg;
  }
  if (evt.round) updateRing(evt.round);
  if (evt.level === "plan" && evt.queries) {
    $("#focus-label").textContent = (evt.queries || []).slice(0, 2).join(" · ");
  }
  if (evt.level === "ok" && evt.msg.includes("学习完成")) refreshAll();
}

function updateRing(round) {
  // 轮换任务在跑时，圆环代表整个轮换的进度；单批的 100 轮只是其中一小段，
  // 拿它当分母会让环反复从 0 跳回满格。
  const rot = state.status?.rotation;
  if (rot && rot.active && rot.total_round_limit) {
    const done = rot.total_rounds_done || 0;
    const pct = Math.min(1, done / rot.total_round_limit);
    $("#ring-fg").style.strokeDashoffset = String(163.4 * (1 - pct));
    $("#round-label").textContent = `轮换 ${done} / ${rot.total_round_limit} 轮`;
    return;
  }
  const total = state.status?.engine?.total_rounds || 1;
  const pct = Math.min(1, round / total);
  const C = 163.4;
  $("#ring-fg").style.strokeDashoffset = String(C * (1 - pct));
  $("#round-label").textContent = `第 ${round} / ${total} 轮`;
}

// SSE 实时通道是否活着。活着时「最近活动」由 SSE 直接推送（零延迟、零额外请求），
// 不再每 6 秒轮询 /api/events/recent——那条轮询是 SSE 上线前的遗留，纯冗余；
// 只有 SSE 断开时才回退成轮询，保证断线时活动流不僵死。
let sseAlive = false;

/* 总览「最近活动」头部的实时状态徽标：让「实时」两个字名副其实——
   绿=SSE 实时推送，琥珀=通道断开、已回退定时刷新。 */
function updateFeedLive() {
  const tag = $("#feed-live");
  if (!tag) return;
  if (sseAlive) {
    tag.textContent = "实时";
    tag.style.color = "var(--green)";
    tag.title = "通过实时通道推送，无需轮询";
  } else {
    tag.textContent = "重连中";
    tag.style.color = "var(--amber)";
    tag.title = "实时通道断开，已回退为定时刷新";
  }
}

function connectSSE() {
  const es = new EventSource("/api/events");
  // 服务端回放历史后会发 history-end，之前收到的都算历史，不播入场动画
  let sseReady = false;
  let firstMsg = true;
  es.onopen = () => {
    sseAlive = true;
    updateFeedLive();
  };
  es.addEventListener("history-end", () => {
    sseReady = true;
  });
  es.onmessage = (e) => {
    try {
      const evt = JSON.parse(e.data);
      // 重连时先清掉旧的历史回放，避免重复堆叠
      if (firstMsg) {
        firstMsg = false;
        const box = $("#learn-stream");
        if (box) box.innerHTML = "";
      }
      // 事件缓冲只保留最近 500 条：原来只 push 从不清理，长时间学习会无限增长
      state.events.push(evt);
      if (state.events.length > 500) state.events.splice(0, state.events.length - 500);
      // 按当前视图把事件送对 feed：总览 → #dash-stream（实时，按 id 去重），
      // 其余视图 → #learn-stream。SSE 早已打开，没必要再让总览每 6 秒轮询一次。
      if (state.view === "dashboard") {
        appendDashEvent(evt, sseReady);
      } else if (state.view === "media") {
        // 多模态页有自己的进度面板；不切过去就看不到解析/转写的实时进度
        pushEvent(evt, $("#media-stream"), sseReady);
      } else {
        pushEvent(evt, null, sseReady);
      }
      if (evt.level === "download" || evt.level === "error") {
        if (evt.msg.includes("下载完成")) toast(evt.msg, "ok");
      }
      // 轮换的换向 / 结束事件要立刻反映到面板，不必等 6 秒轮询；
      // 面板不可见时不必刷（切回该页会重新拉一次）。
      if (evt.rotation && state.view === "learn") scheduleRotationRefresh();
    } catch (_) {}
  };
  es.onerror = () => {
    sseAlive = false;
    updateFeedLive();
    es.close();
    sseReady = false;
    setTimeout(connectSSE, 4000);
  };
}

/* ---------------- 总览 ---------------- */
/* 内容没变就不动 DOM —— 轮询刷新时避免整块重绘导致文字闪动 */
function setHTML(el, html) {
  if (!el) return false;
  if (el.__lastHTML === html) return false;
  el.__lastHTML = html;
  el.innerHTML = html;
  return true;
}

let dashEvtId = 0;

async function loadDashboard(st, forceEvents = false) {
  if (!st) {
    try {
      st = await api("/api/status");
    } catch (e) {
      toast("状态获取失败：" + e.message, "err");
      return;
    }
  }
  state.status = st;
  const s = st.stats;
  $("#s-points").textContent = s.points;
  $("#s-new").textContent = `24 小时新增 ${s.new_24h}`;
  $("#s-docs").textContent = s.documents;
  $("#s-res").textContent = s.resources;
  $("#s-res-disk").textContent = `占用 ${s.disk_gb} GB`;
  $("#s-sessions").textContent = s.sessions;

  const tl = $("#topic-list");
  if (!s.topics.length) {
    setHTML(tl, '<div class="empty">还没有任何学习记录</div>');
  } else {
    const max = Math.max(...s.topics.map((t) => t.count));
    setHTML(
      tl,
      s.topics
        .slice(0, 8)
        .map(
          (t) => `<div style="margin-bottom:11px">
            <div class="rowflex" style="justify-content:space-between;font-size:12.5px">
              <span>${esc(t.topic)}</span>
              <span class="mono dim">${t.count}</span>
            </div>
            <div class="bar"><i style="width:${(t.count / max) * 100}%"></i></div>
          </div>`
        )
        .join("")
    );
  }
  paintTopbar(st);
  renderNextSteps(st);
  renderMilestones(s.points);
  // SSE 在线时活动流已实时推送，别再每 6 秒轮询 /api/events/recent；
  // 只有初次/强制加载、或 SSE 断开回退时才拉一次。
  if (forceEvents || !sseAlive) await syncDashEvents(forceEvents);
}

/* 增量同步「最近活动」：只追加新事件，绝不重建已有行。
   之前每 6 秒清空重渲染，导致每行重播入场动画，视觉上一直在抖。 */
async function syncDashEvents(force) {
  let evts;
  try {
    evts = await api("/api/events/recent?limit=30");
  } catch (_) {
    return;
  }
  const box = $("#dash-stream");
  if (!box) return;

  if (force || dashEvtId === 0) {
    box.innerHTML = "";
    if (!evts.length) {
      box.innerHTML = `<div class="empty" style="text-align:left;line-height:1.85;padding:10px 2px">
        <div style="font-weight:600;margin-bottom:4px">还没有活动</div>
        <div>去「自主进化」点一次「开始进化学习」，这里会实时滚出它检索、阅读、提炼的每一步。</div>
      </div>`;
      return;
    }
    evts.forEach((e) => pushEvent(e, box, false));
    box.scrollTop = box.scrollHeight;
    dashEvtId = evts[evts.length - 1].id || 0;
    return;
  }

  const fresh = evts.filter((e) => (e.id || 0) > dashEvtId);
  if (!fresh.length) return;
  const empty = box.querySelector(".empty");
  if (empty) empty.remove();
  fresh.forEach((e) => pushEvent(e, box, true));
  dashEvtId = fresh[fresh.length - 1].id || dashEvtId;
}

/* SSE 实时推送进总览「最近活动」：按 id 去重后追加。
   SSE 重连会重放历史、初次加载也会拉 recent，两边都靠 id 兜底，不会重复堆叠。 */
function appendDashEvent(evt, animate) {
  const box = $("#dash-stream");
  if (!box) return;
  if ((evt.id || 0) <= dashEvtId) return; // 已展示过（初次加载 / 历史回放）
  pushEvent(evt, box, animate);
  dashEvtId = evt.id || dashEvtId;
}

/* 本地启发式兜底：后端 /api/analytics/suggestions 拉不到时，
   至少按规模给出和以前一致的引导（弱于后端版：看不到久未学/薄弱方向等）。 */
function localNextSteps(st) {
  const s = st.stats;
  const items = [];
  const add = (level, icon, title, desc, view, btn) =>
    items.push({ level, icon, title, detail: desc, view, btn });

  if (s.scale_warn) {
    add("warn", "!", "知识库规模提醒", s.scale_warn, "knowledge", "去整理");
  }

  if (s.points === 0) {
    add("act", "◐", "先让它学一个方向", "去「自主进化」填一个具体方向，点开始。3 轮约 10 分钟就有第一批知识。", "learn", "去学习");
  } else if (s.points < 30) {
    add("act", "◑", "知识还比较薄", `目前 ${s.points} 条，建议再跑几轮加深，或换个角度补盲区。`, "learn", "继续学");
  } else if (s.points < 120) {
    add("act", "◕", "可以检验一下学得怎么样", "去「检验所学」问几个真问题。答不上来的地方，就是下一步该补的方向。", "chat", "去提问");
  } else {
    add("act", "●", "素材够了，可以试试权重进化", `已积累 ${s.points} 条知识。去「权重进化」把它们压进模型参数，得到真正「学会」的版本。`, "finetune", "去微调");
  }

  if (!st.finetune.ready) {
    add("info", "⚙", "微调环境还没装", "想用权重层进化，需要先装 PyTorch 等依赖（约 3GB，装一次）。", "finetune", "去安装");
  }
  if (s.resources === 0 && st.config.auto_download) {
    add("info", "⤓", "还没有自主下载过资源", "学习时它会自动扫描对口的模型和数据集，预算内自己下。也可以手动指定。", "resources", "看看");
  }
  add("info", "✎", "随时可以问它", "它只用学过的知识回答并标出处，不会拿模型记忆糊弄你。", "chat", "去问");
  return items.slice(0, 3);
}

/* ---------------- 学习建议（引导） ---------------- */
let lastSuggFetch = 0;
async function refreshSuggestions(force = false) {
  // 建议按知识库现状生成，没必要 6 秒刷一次；30 秒 + 切回总览时刷新足够
  const now = Date.now();
  if (!force && now - lastSuggFetch < 30000) return;
  lastSuggFetch = now;
  try {
    const d = await api("/api/analytics/suggestions");
    state.suggestions = (d && d.items) || [];
    if (state.view === "dashboard" && state.status) renderNextSteps(state.status);
  } catch (_) {}
}

/* ---------------- 知识库健康度（总览卡片） ---------------- */
let lastHealthFetch = 0;
async function refreshHealth(force = false) {
  // 健康度是纯算术（服务端随聚合缓存一起命中），但没必要 6 秒刷一次；15 秒足够
  const now = Date.now();
  if (!force && now - lastHealthFetch < 15000) return;
  lastHealthFetch = now;
  let h;
  try {
    h = await api("/api/analytics/health");
  } catch (_) {
    return;
  }
  try {
    renderHealth(h);
  } catch (_) {}
}

function renderHealth(h) {
  const box = $("#health-dims");
  const weak = $("#health-weak");
  if (!box || !weak) return;
  if (!h || h.empty) {
    setHTML($("#health-gauge"), "");
    setHTML($("#health-level"), "空库");
    setHTML(box, `<div class="empty" style="text-align:left;line-height:1.8;padding:2px 2px 8px">
      <div style="font-weight:600;margin-bottom:4px">知识库还是空的，健康度无从谈起</div>
      <div>去「自主进化」定一个具体方向，学几轮之后这里会给出一整套评分：规模、可信度、信源多样性、学习新鲜度——哪项最低，就说明下一步该补什么。</div>
    </div>`);
    setHTML(weak, `<button class="btn primary sm" data-goto="learn">去定方向 →</button>`);
    return;
  }
  const dims = h.dims || [];
  if (!dims.length) {
    setHTML($("#health-gauge"), drawGauge(150, h.score || 0, "健康度"));
    setHTML($("#health-level"), h.level || "—");
    setHTML(box, '<div class="empty">评分计算中…</div>');
    setHTML(weak, "");
    return;
  }
  setHTML($("#health-gauge"), drawGauge(150, h.score, "健康度"));
  setHTML($("#health-level"), h.level);
  setHTML(
    box,
    h.dims
      .map((d) => {
        const col = d.score >= 70 ? "var(--green)" : d.score >= 40 ? "var(--accent)" : "var(--orange, #e0a23b)";
        return `<div class="health-dim">
          <div class="rowflex" style="justify-content:space-between;font-size:12.5px">
            <span>${esc(d.label)}</span>
            <span class="mono dim">${d.score}</span>
          </div>
          <div class="bar"><i style="width:${d.score}%;background:${col}"></i></div>
          <div class="hint" style="margin:2px 0 0">${esc(d.detail)}</div>
        </div>`;
      })
      .join("")
  );
  const w = h.weakest || {};
  setHTML(
    weak,
    `最薄弱：<b>${esc(w.label || "—")}</b>（${w.score ?? 0} 分）· ${esc(w.hint || "")}
     <button class="btn sm" data-goto="${esc(w.view || "learn")}" style="margin-left:8px">${esc(w.btn || "去补")} →</button>`
  );
}

/* ---------------- 新手上路引导 ---------------- */
const OB_STEPS = [
  {
    icon: "◐",
    title: "第一步 · 定一个具体方向",
    desc: "方向越具体，学得越深。「光固化 3D 打印的收缩补偿模型」就比「3D 打印」好得多。去「自主进化」填好方向，预设里也有现成的可以点。",
    action: { view: "learn", btn: "去填方向" },
  },
  {
    icon: "▶",
    title: "第二步 · 开始进化学习",
    desc: "点「开始进化学习」。每一轮它都会自己诊断缺口 → 多源检索 → 阅读 → 提炼入库，约 2-4 分钟。选 3 轮快速摸底，十分钟后总览就有数据了。",
    action: { view: "learn", btn: "去开始" },
  },
  {
    icon: "❖",
    title: "第三步 · 看它学得怎么样",
    desc: "「分析」页有增长曲线、方向分布、可信度直方图、学习活跃度热力图和主题语义网络；「检验所学」可以拿真问题考它——答不上来的地方就是下一步该补的。",
    action: { view: "analytics", btn: "去看分析" },
  },
];
let obStep = 0;
const OB_KEY = "evomind.onboarded.v1";

function renderOnboarding() {
  const s = OB_STEPS[obStep];
  setHTML(
    $("#ob-step"),
    `<div style="display:flex;gap:14px;align-items:flex-start">
      <div class="ob-ico">${s.icon}</div>
      <div style="flex:1">
        <div class="ob-title">${esc(s.title)}</div>
        <div class="ob-desc">${esc(s.desc)}</div>
        <div class="ob-action"><button class="btn sm" data-ob-action="${s.action.view}">${esc(s.action.btn)} →</button></div>
      </div>
    </div>`
  );
  setHTML(
    $("#ob-dots"),
    OB_STEPS.map((_, i) => `<i class="${i === obStep ? "on" : ""}" data-ob-dot="${i}"></i>`).join("")
  );
  $$("#ob-step [data-ob-action]").forEach((b) =>
    b.addEventListener("click", () => {
      closeOnboarding();
      switchView(b.dataset.obAction);
    })
  );
  $$("#ob-dots [data-ob-dot]").forEach((d) =>
    d.addEventListener("click", () => {
      obStep = Number(d.dataset.obDot);
      renderOnboarding();
    })
  );
  $("#ob-prev").disabled = obStep === 0;
  $("#ob-next").textContent = obStep === OB_STEPS.length - 1 ? "开始使用" : "下一步";
}

function openOnboarding() {
  obStep = 0;
  renderOnboarding();
  $("#onboarding").hidden = false;
}

function closeOnboarding() {
  try { localStorage.setItem(OB_KEY, "1"); } catch (_) {}
  $("#onboarding").hidden = true;
}

function maybeShowOnboarding() {
  try {
    if (localStorage.getItem(OB_KEY)) return;
  } catch (_) {}
  // 深链直达某个具体视图（#knowledge、#knowledge/<id>、分享链接）的访客
  // 多半不是第一次用，不要拿引导浮层挡住他想看的东西
  const target = location.hash.slice(1).split("/")[0];
  if (target && target !== "dashboard") return;
  // 总览数据到位后再弹，避免一上来挡着 loading
  setTimeout(openOnboarding, 600);
}

function renderNextSteps(st) {
  // 后端建议（数据驱动：久未学/薄弱方向/低可信度/信源单一/规模引导）优先，
  // 拉取失败才退回本地启发式
  const items =
    state.suggestions && state.suggestions.length
      ? state.suggestions
      : localNextSteps(st);

  const html = items
    .slice(0, 3)
    .map(
      (i) => `<div class="row" style="display:flex;gap:12px;align-items:flex-start">
        <span class="ns-level ${i.level || "info"}" title="${i.level === "act" ? "建议行动" : i.level === "warn" ? "需要留意" : "提示"}"></span>
        <div style="flex:1">
          <div style="font-size:13.8px;font-weight:600">${esc(i.title)}</div>
          <div class="detail" style="margin-top:4px">${esc(i.detail || i.desc || "")}</div>
        </div>
        <button class="btn sm" data-goto="${i.view}">${esc(i.btn)}</button>
      </div>`
    )
    .join("");

  // 内容没变就不重建，避免按钮每 6 秒闪一下
  setHTML($("#next-steps"), html);
}

/* ---------------- 成长里程碑（增强引导） ---------------- */
// 把「知识库到哪个阶段了、下一步该干嘛」画成一条带刻度里程碑的进度条。
// 用户不必自己算「还差多少条才能去微调」——差几条第几个里程碑直接写出来，
// 点里程石还能跳到对应功能页。数据来自 /api/status 的 stats.points（已被版本缓存，
// 轮询几乎零成本，所以这个进度条可以放心每次状态刷新都重算）。
const MILESTONES = [
  { n: 30, label: "检验所学", view: "chat" },
  { n: 120, label: "权重进化", view: "finetune" },
  { n: 500, label: "规模成型", view: "analytics" },
  { n: 2000, label: "知识上限", view: "knowledge" },
];
const MS_CAP = 2000;

function renderMilestones(points) {
  const box = $("#milestone");
  if (!box) return;
  points = points | 0;
  const frac = Math.max(0, Math.min(points, MS_CAP)) / MS_CAP;
  const pct = (frac * 100).toFixed(1);
  const ticks = MILESTONES.map((m) => {
    const left = (m.n / MS_CAP) * 100;
    const reached = points >= m.n;
    return `<span class="ms-tick ${reached ? "reached" : ""}" style="left:${left}%"></span>`;
  }).join("");
  const legend = MILESTONES.map((m) => {
    const reached = points >= m.n;
    return `<span class="ms-chip ${reached ? "reached" : ""}" data-goto="${m.view}" title="点击去「${m.label}」">
      <b>${m.n}</b> ${m.label}${reached ? " ✓" : ""}</span>`;
  }).join("");
  const next = MILESTONES.find((m) => points < m.n);
  let guide;
  if (points === 0) {
    guide = "还没有知识。去「自主进化」定个方向，跑 3 轮就能站上第一个里程碑。";
  } else if (!next) {
    guide = "已抵达全部里程碑！知识库规模与质量都足以支撑权重进化。";
  } else {
    guide = `再学 <b>${next.n - points}</b> 条即可解锁「${next.label}」（共 ${next.n} 条）。`;
  }
  setHTML(
    box,
    `<div class="ms-head"><span>当前 <b>${points}</b> / ${MS_CAP} 条</span><span class="ms-pct">${pct}%</span></div>
     <div class="ms-track">
       <div class="ms-fill" style="width:${pct}%"></div>
       <div class="ms-cursor" style="left:${pct}%"></div>
       ${ticks}
     </div>
     <div class="ms-legend">${legend}</div>
     <div class="ms-guide">${guide}</div>`
  );
  const lvl = $("#ms-level");
  if (lvl) lvl.textContent = next ? `距 ${next.label} 还差 ${next.n - points}` : "已全达成";
}

function paintTopbar(st) {
  learnActive = !!st.engine.running;
  $("#m-points").textContent = st.stats.points;
  $("#m-docs").textContent = st.stats.documents;
  $("#m-disk").textContent = st.stats.disk_gb + " GB";
  const running = st.engine.running;
  $("#m-state").textContent = running ? "学习中" : "空闲";
  $("#m-state").style.color = running ? "var(--green)" : "";

  // 响应延迟徽标：绿（≤20ms）/ 黄（≤60ms）/ 红（更慢），直接反映轮询优化效果
  const rms = st.response_ms;
  const lat = $("#m-latency");
  if (lat && typeof rms === "number") {
    lat.textContent = rms.toFixed(0) + " ms";
    lat.style.color =
      rms <= 20 ? "var(--green)" : rms <= 60 ? "var(--amber, #e0a23b)" : "var(--red, #d9534f)";
  }

  const llmOk = st.llm.healthy;
  const checking = !!st.llm.checking && !llmOk;
  $("#dot-llm").className = "dot " + (llmOk ? "on" : checking ? "warn" : "off");
  $("#txt-llm").textContent = llmOk ? "模型在线" : checking ? "检测中…" : "模型离线";
  const dot = $("#dot-llm");
  if (dot) {
    dot.title = llmOk
      ? (st.llm.models || []).length + " 个模型可用"
      : checking
        ? "正在探测本地 Ollama（首次探测可能要等连接超时）"
        : "连不上 Ollama：确认它已启动（ollama serve），或在设置里改服务地址";
  }

  $("#dot-gpu").className = "dot " + (st.finetune.cuda ? "on" : "warn");
  $("#txt-gpu").textContent = st.finetune.cuda
    ? (st.finetune.gpu || "").replace("NVIDIA GeForce ", "")
    : st.finetune.ready
      ? "CUDA 不可用"
      : "微调未装";

  $("#btn-start").disabled = running || learnStarting || !!st.rotation?.active;
  $("#btn-stop").disabled = !running && !st.rotation?.active;
  $("#stage-tag").textContent = STAGE_TEXT[st.engine.stage] || st.engine.stage;
  if (st.engine.running) updateRing(st.engine.round);
}

/* ---------------- 学习 ---------------- */
let learnStarting = false;
let learnActive = false;
async function startLearning(chosen) {
  if (learnStarting || learnActive || state.status?.engine?.running) return;
  if (state.status?.rotation?.active) return toast("方向轮换正在运行，先暂停或结束它", "err");
  const direction = chosen || $("#direction").value.trim();
  if (!direction) return toast("先写一个学习方向", "err");
  $("#direction").value = direction;
  $$(".direction-choice").forEach((el) => el.classList.toggle("selected", el.textContent === direction));
  const rounds = parseInt($("#rounds").value, 10);
  learnStarting = true;
  $("#btn-start").disabled = true;
  try {
    const r = await api("/api/learn/start", { method: "POST", body: { direction, rounds } });
    if (!r.ok) return toast(r.msg || "启动失败", "err");
    learnActive = true;
    $("#learn-stream").innerHTML = "";
    if (state.status) state.status.engine.running = true;
    toast("已启动进化学习", "ok");
    $("#btn-start").disabled = true;
    $("#btn-stop").disabled = false;
  } catch (e) {
    toast("启动失败：" + e.message, "err");
  } finally {
    learnStarting = false;
    const running = learnActive || !!state.status?.engine?.running;
    $("#btn-start").disabled = running;
  }
}
$("#btn-start").addEventListener("click", () => startLearning());

$("#btn-stop").addEventListener("click", async () => {
  try {
    await api("/api/learn/stop", { method: "POST" });
    autoOn = false;
    await loadAuto();
    toast("已停止持续调度，当前请求完成后收尾");
  } catch (e) { toast(e.message, "err"); }
});

$$("#presets .chip").forEach((c) =>
  c.addEventListener("click", () => {
    $("#direction").value = c.textContent;
    $("#direction-seed").value = c.textContent;
  })
);

let directionChoices = [];
let directionSeed = "";
let directionsBusy = false;
async function refreshDirections() {
  if (directionsBusy) return [];
  const seed = $("#direction-seed").value.trim();
  if (!seed) { toast("先填一个预设领域", "err"); return []; }
  directionsBusy = true;
  $("#btn-directions").disabled = true;
  try {
    const r = await api("/api/learn/directions", {
      method: "POST", body: { seed, exclude: directionSeed === seed ? directionChoices : [] },
    });
    // A result for an earlier input must not overwrite a newly typed seed.
    if ($("#direction-seed").value.trim() !== seed) return [];
    directionChoices = r.items || [];
    directionSeed = seed;
    $("#direction-options").innerHTML = directionChoices.map((t, i) =>
      `<button class="direction-choice" data-direction-index="${i}" type="button">${esc(t)}</button>`
    ).join("");
    return directionChoices;
  } catch (e) { toast("刷新失败：" + e.message, "err"); return []; }
  finally { directionsBusy = false; $("#btn-directions").disabled = false; }
}
$("#btn-directions").addEventListener("click", refreshDirections);
$("#direction-options").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-direction-index]");
  if (!btn) return;
  const chosen = directionChoices[Number(btn.dataset.directionIndex)];
  if (chosen) {
    $("#direction").value = chosen;
    $$(".direction-choice").forEach((el) => el.classList.toggle("selected", el === btn));
  }
});
let choosingRandom = false;
async function chooseRandomDirection() {
  if (choosingRandom) return;
  choosingRandom = true;
  $("#btn-random-learn").disabled = true;
  try {
    const choices = directionSeed === $("#direction-seed").value.trim() && directionChoices.length
      ? directionChoices : await refreshDirections();
    if (choices.length) {
      const direction = choices[Math.floor(Math.random() * choices.length)];
      $("#direction").value = direction;
      $$(".direction-choice").forEach((el) => el.classList.toggle("selected", el.textContent === direction));
      toast("方向已选好，调整轮数后点击「开始进化学习」");
    }
  } finally {
    choosingRandom = false;
    $("#btn-random-learn").disabled = false;
  }
}
$("#btn-random-learn").addEventListener("click", chooseRandomDirection);

/* ---------------- 持续自学 ---------------- */
let autoOn = false;

async function loadAuto() {
  try {
    const a = await api("/api/auto");
    autoOn = a.enabled;
    paintAuto(a);
    if (!$("#direction").value && a.direction) $("#direction").value = a.direction;
  } catch (_) {}
}

function paintAuto(a) {
  const btn = $("#btn-auto");
  btn.textContent = a.enabled ? "已开启 · 关闭" : "开启";
  btn.title = a.enabled ? "点击关闭持续自学" : "开启后空闲一段时间会自动接着学";
  btn.className = "btn sm" + (a.enabled ? " primary" : "");
  const d = $("#auto-desc");
  if (a.enabled) {
    // 状态用按钮的 primary 态表达，说明文字保持中性色，别整段变绿抢视线
    const wait = a.waiting ? ` · 等待：${esc(a.waiting)}` : a.next_run ? ` · 约${Math.max(0, Math.ceil(a.next_run - Date.now() / 1000))}秒后继续` : "";
    d.innerHTML = `沿用「${esc(a.direction || "上次方向")}」，间隔 ${a.idle_seconds} 秒 · 每次 ${a.auto_rounds} 轮 · 已自动学 ${a.runs} 次${wait}`;
    d.style.color = "";
  } else {
    d.textContent = "每段学习结束后最多等待30秒继续；导入、OCR、微调或炼化期间等待，停止学习会关闭调度。";
    d.style.color = "";
  }
}

let autoSettingsFilled = false;
function autoOptions() {
  const idle_seconds = Number($("#auto-seconds").value);
  const auto_rounds = Number($("#auto-rounds").value);
  if (!Number.isInteger(idle_seconds) || idle_seconds < 1 || idle_seconds > 30) throw new Error("学习间隔请输入1到30之间的整数秒");
  if (!Number.isInteger(auto_rounds) || auto_rounds < 1 || auto_rounds > 100) throw new Error("每次轮数请输入1到100之间的整数");
  return { idle_seconds, auto_rounds };
}
function fillAutoSettings(a) {
  if (autoSettingsFilled) return;
  $("#auto-seconds").value = a.idle_seconds || 15;
  $("#auto-rounds").value = a.auto_rounds || 2;
  if (state.status?.config?.learning_seed) $("#direction-seed").value = state.status.config.learning_seed;
  autoSettingsFilled = true;
}
$("#btn-auto-save").addEventListener("click", async () => {
  try {
    await api("/api/config", { method: "POST", body: autoOptions() });
    await loadAuto();
    toast("持续自学设置已保存", "ok");
  } catch (e) { toast(e.message, "err"); }
});

$("#btn-auto").addEventListener("click", async () => {
  const direction = $("#direction").value.trim();
  if (!autoOn && !direction) {
    const last = state.status?.config?.last_direction;
    if (!last) return toast("先填一个学习方向，再开启持续自学", "err");
  }
  try {
    const r = await api("/api/auto", {
      method: "POST",
      body: { enabled: !autoOn, direction: direction || undefined,
        ...(!autoOn ? autoOptions() : {}) },
    });
    autoOn = r.enabled;
    paintAuto(r);
    toast(r.enabled ? "持续自学已开启" : "持续自学已关闭", r.enabled ? "ok" : "");
  } catch (e) {
    toast(e.message, "err");
  }
});

/* ---------------- 学习方向自动更换（轮换） ---------------- */
let rotationBusy = false;
let rotationSettingsFilled = false;
let rotationPreviewShown = false;
let rotationSummaryRun = "";
let rotationRefreshTimer = 0;
// 预览过的子方向：启动时若母方向没变就沿用，避免「预览看到的」和「实际学的」是两批
let rotPreviewItems = [];
let rotPreviewSeed = "";

function scheduleRotationRefresh() {
  clearTimeout(rotationRefreshTimer);
  rotationRefreshTimer = setTimeout(() => loadRotation(), 400);
}

// 后端 /api/rotation 会直接给 status_text；这里只留一份兜底，
// 供旧缓存状态或本地构造的对象使用，正常路径不再各自维护一套文案。
const ROT_STATUS = {
  total_rounds: "已完成", stopped: "已手动结束", error: "异常结束",
  no_plan: "无可用子方向", no_direction: "无可用子方向",
};
const ROT_STAGE = {
  idle: "空闲", starting: "启动中", splitting: "拆解子方向", switching: "换方向",
  learning: "学习中", pausing: "暂停中", paused: "已暂停", stopping: "结束中",
  waiting_model: "等待模型",
  done: "已结束", stopped: "已结束",
};

function rotNumber(sel, label, min, max) {
  const v = Number($(sel).value);
  if (!Number.isInteger(v) || v < min || v > max) {
    throw new Error(`${label}请输入 ${min} 到 ${max} 之间的整数`);
  }
  return v;
}

function paintRotation(r) {
  if (!r) return;
  const active = !!r.active;
  $("#rot-tag").textContent = r.status_text
    || (active
      ? (r.paused ? "已暂停" : (ROT_STAGE[r.stage] || "进行中"))
      : (ROT_STATUS[r.stop_reason] || "未启动"));
  const startBtn = $("#btn-rot-start");
  startBtn.disabled = active || rotationBusy;
  startBtn.title = active ? "已有轮换任务在运行，先暂停或结束" : "按当前参数开始自动轮换";
  $("#btn-rot-plan").disabled = rotationBusy;
  $("#btn-rot-plan").title = "只拆解子方向，不启动学习";
  $("#btn-rot-pause").disabled = !active || r.paused;
  $("#btn-rot-pause").title = !active
    ? "没有运行中的任务"
    : (r.paused ? "已经是暂停状态" : "让当前这一轮跑完再停");
  $("#btn-rot-resume").disabled = !active || !r.paused;
  $("#btn-rot-resume").title = !active
    ? "没有可恢复的任务"
    : (r.paused ? "从剩余轮次继续" : "任务正在运行");
  $("#btn-rot-stop").disabled = !active;
  $("#btn-rot-stop").title = active ? "结束任务并输出各方向汇总" : "没有运行中的任务";
  const skipBtn = $("#btn-rot-skip");
  if (skipBtn) {
    skipBtn.disabled = !active || !r.current || !!r.skip_requested;
    skipBtn.title = !active
      ? "没有运行中的任务"
      : (!r.current ? "还没有选中方向" : "放弃该方向剩余轮次，立刻换下一个");
  }
  if (!$("#rot-seed").value && (r.seed || r.last_run?.seed)) {
    $("#rot-seed").value = r.seed || r.last_run.seed;
  }

  // 预览结果先占着状态栏，别让 6 秒轮询把它擦掉（但计划/历史照常渲染）
  const keepPreview = !active && !r.stop_reason && rotationPreviewShown;

  let text;
  if (active) {
    const left = Math.max(0, (r.rounds_per_direction || 0) - (r.rounds_in_current || 0));
    const head = r.paused ? "已暂停" : (ROT_STAGE[r.stage] || "进行中");
    text = `<b>${head}</b> · 母方向「${esc(r.seed)}」`;
    if (r.current) text += ` · 当前「${esc(r.current)}」还剩 ${left} 轮`;
    text += ` · 总计 ${r.total_rounds_done}/${r.total_round_limit} 轮`;
    if (r.direction_total) text += ` · 子方向 ${r.direction_index || 0}/${r.direction_total}`;
    if (r.switch_count) text += ` · 已换向 ${r.switch_count} 次`;
    if (r.retry_in) text += ` · ${r.retry_in} 秒后自动重试`;
    else if (r.skip_requested) text += " · 正在跳过当前方向";
    else if (r.blocked) text += " · 等待其他任务结束";
    else if (r.failures) text += ` · 连续 ${r.failures} 次未推进`;
  } else if (r.stop_reason) {
    text = `<b>${ROT_STATUS[r.stop_reason] || "已结束"}</b> · 母方向「${esc(r.seed)}」 · `
      + `共 ${r.total_rounds_done} 轮 / 上限 ${r.total_round_limit} 轮`;
  } else if (r.last_run) {
    text = `<b>上次任务</b> · 「${esc(r.last_run.seed)}」 · `
      + `${r.last_run.total_rounds}/${r.last_run.total_limit} 轮`;
  } else {
    text = "尚未启动";
  }
  if (!keepPreview) $("#rot-status").innerHTML = text;

  const pct = Math.max(0, Math.min(100, 100 * (r.progress || 0)));
  const showBar = active || (r.total_rounds_done || 0) > 0;
  const bar = $("#rot-bar");
  bar.hidden = !showBar;
  bar.setAttribute("role", "progressbar");
  bar.setAttribute("aria-label", "轮换总进度");
  bar.setAttribute("aria-valuemin", "0");
  bar.setAttribute("aria-valuemax", String(r.total_round_limit || 0));
  bar.setAttribute("aria-valuenow", String(r.total_rounds_done || 0));
  $("#rot-bar-fill").style.width = (showBar ? pct : 0) + "%";

  const plan = (r.plan && r.plan.length) ? r.plan : (r.last_run?.directions || []);
  $("#rot-plan-list").innerHTML = plan.map((e) => {
    const st = e.status || "pending";
    const meta = st === "rejected"
      ? "越界已丢弃"
      : `${e.rounds || 0} 轮 · ${e.points || 0} 条`;
    return `<div class="rot-item ${esc(st)}" title="${esc(e.note || e.direction || "")}"
      aria-current="${st === "active" ? "true" : "false"}">
      <span class="dot"></span><span class="name">${esc(e.direction)}</span>
      <span class="meta">${meta}</span></div>`;
  }).join("");

  // /api/status 里的 rotation 不带 history（那是要查库的字段，不能进 6 秒轮询）。
  // 只有明确给了数组才重画，否则保留当前内容——不然每次轮询都会把「最近任务」刷掉。
  if (Array.isArray(r.history)) paintRotationHistory(r.history);
}

function paintRotationHistory(history) {
  const box = $("#rot-history");
  if (!box) return;
  if (!history.length) {
    box.hidden = true;
    $("#rot-history-list").innerHTML = "";
    return;
  }
  box.hidden = false;
  $("#rot-history-list").innerHTML = history.map((h) => {
    const when = h.started_at
      ? new Date(h.started_at * 1000).toLocaleString("zh-CN", { hour12: false })
      : "—";
    const text = h.status_text || ROT_STATUS[h.status] || h.status || "";
    return `<div class="rot-hist-row">
      <span class="name">${esc(h.seed)}</span>
      <span class="meta">${h.total_rounds || 0}/${h.total_limit || 0} 轮 · ${esc(text)} · ${esc(when)}</span>
    </div>`;
  }).join("");
}

function renderRotationSummary(summ) {
  const box = $("#rot-summary");
  const actions = $("#rot-summary-actions");
  if (!summ || !(summ.directions || []).length) {
    box.hidden = true;
    box.innerHTML = "";
    if (actions) actions.hidden = true;
    return;
  }
  const switchCount = summ.switch_count != null
    ? summ.switch_count
    : (summ.switches || []).filter((s) => s && s.from).length;
  const secs = summ.elapsed_seconds || 0;
  const cost = secs ? ` · 耗时 ${Math.floor(secs / 60)} 分 ${secs % 60} 秒` : "";
  const skipped = summ.skipped_count || 0;
  const drop = skipped ? ` · 丢弃越界方向 ${skipped} 个` : "";
  const lines = [
    `<div class="rot-head">方向轮换汇总 · 母方向「${esc(summ.seed)}」</div>`,
    `总 ${summ.total_rounds} 轮 / 上限 ${summ.total_limit} · 每方向 ${summ.rounds_per_direction} 轮`
      + ` · 切换 ${switchCount} 次 · 新增 ${summ.points_total} 条知识${cost}${drop}`,
  ];
  (summ.directions || []).forEach((d) => {
    const short = d.rounds < (summ.rounds_per_direction || 0) ? "（未学满）" : "";
    lines.push(`· ${esc(d.direction)}：${d.rounds} 轮 / ${d.points} 条${short}`);
    (d.highlights || []).slice(0, 2).forEach((h) => lines.push(`　　· ${esc(String(h).slice(0, 80))}`));
  });
  box.innerHTML = lines.join("\n");
  box.hidden = false;
  if (actions) actions.hidden = false;
}

$("#btn-rot-copy").addEventListener("click", async () => {
  const box = $("#rot-summary");
  const text = (box && (box.innerText || box.textContent) || "").trim();
  if (!text) return toast("还没有可复制的汇总", "err");
  try {
    if (typeof navigator === "undefined" || !navigator.clipboard) {
      throw new Error("clipboard unavailable");
    }
    await navigator.clipboard.writeText(text);
    toast("汇总已复制到剪贴板", "ok");
  } catch (_) {
    // 非 https 或浏览器禁用了剪贴板权限：给出可执行的替代动作，而不是静默失败
    toast("浏览器不允许自动复制，请手动选中汇总文本", "err");
  }
});

async function loadRotation(force = false) {
  if (rotationBusy && !force) return;
  try {
    const r = await api("/api/rotation");
    paintRotation(r);
    if (!r.active && r.stop_reason && rotationSummaryRun !== r.run_id) {
      rotationSummaryRun = r.run_id || "last";
      const s = await api("/api/rotation/summary");
      renderRotationSummary(s.summary);
    }
  } catch (_) {}
}

function fillRotationSettings(cfg) {
  if (rotationSettingsFilled || !cfg) return;
  if (cfg.rotation_rounds_per_direction) $("#rot-per").value = cfg.rotation_rounds_per_direction;
  if (cfg.rotation_total_rounds) $("#rot-total").value = cfg.rotation_total_rounds;
  if (cfg.rotation_subdirection_count) $("#rot-count").value = cfg.rotation_subdirection_count;
  if (cfg.rotation_recent_window) $("#rot-window").value = cfg.rotation_recent_window;
  $("#rot-boundary").value = cfg.rotation_boundary_check === false ? "0" : "1";
  if (!$("#rot-seed").value && cfg.learning_seed) $("#rot-seed").value = cfg.learning_seed;
  rotationSettingsFilled = true;
}

$("#btn-rot-start").addEventListener("click", async () => {
  if (rotationBusy) return;
  const seed = ($("#rot-seed").value || $("#direction").value).trim();
  if (!seed) return toast("先填一个大致的学习方向", "err");
  let body;
  try {
    body = {
      seed,
      rounds_per_direction: rotNumber("#rot-per", "每个子方向轮数", 1, 2000),
      total_rounds: rotNumber("#rot-total", "总轮次上限", 1, 100000),
      subdirection_count: rotNumber("#rot-count", "子方向数量", 2, 20),
    };
    const windowSize = rotNumber("#rot-window", "不重复窗口", 1, 20);
    await api("/api/config", {
      method: "POST",
      body: {
        rotation_recent_window: windowSize,
        rotation_boundary_check: $("#rot-boundary").value === "1",
      },
    });
  } catch (e) {
    return toast(e.message, "err");
  }
  if (body.total_rounds < body.rounds_per_direction) {
    toast("总轮次比每方向轮数还少：只会学完一个方向的一部分", "");
  }
  rotationBusy = true;
  $("#btn-rot-start").disabled = true;
  try {
    if (rotPreviewSeed === seed && rotPreviewItems.length) body.items = rotPreviewItems;
    const r = await api("/api/rotation/start", { method: "POST", body });
    if (!r.ok) return toast(r.msg || "启动失败", "err");
    rotationPreviewShown = false;
    rotationSummaryRun = "";
    $("#rot-summary").hidden = true;
    toast("已启动方向轮换：正在确定子方向", "ok");
    await loadRotation(true);
  } catch (e) {
    toast("启动失败：" + e.message, "err");
  } finally {
    rotationBusy = false;
    $("#btn-rot-start").disabled = !!state.status?.rotation?.active;
  }
});

$("#btn-rot-plan").addEventListener("click", async () => {
  if (rotationBusy) return;
  const seed = ($("#rot-seed").value || $("#direction").value).trim();
  if (!seed) return toast("先填一个大致的学习方向", "err");
  let count;
  try {
    count = rotNumber("#rot-count", "子方向数量", 2, 20);
  } catch (e) {
    return toast(e.message, "err");
  }
  rotationBusy = true;
  $("#btn-rot-plan").disabled = true;
  try {
    const r = await api("/api/rotation/plan", { method: "POST", body: { seed, count } });
    const items = r.items || [];
    $("#rot-plan-list").innerHTML = items.length
      ? items.map((t) => `<div class="rot-item pending"><span class="dot"></span>
          <span class="name">${esc(t)}</span><span class="meta">预览</span></div>`).join("")
      : `<div class="hint">没拆出可用方向，换一个更明确的领域名试试</div>`;
    rotPreviewItems = items;
    rotPreviewSeed = seed;
    $("#rot-status").innerHTML = `预览：将从「${esc(seed)}」拆出 ${items.length} 个子方向`
      + `（尚未启动；点「开始自动轮换」会沿用这批方向）`;
    rotationPreviewShown = true;
  } catch (e) {
    toast("预览失败：" + e.message, "err");
  } finally {
    rotationBusy = false;
    $("#btn-rot-plan").disabled = false;
  }
});

async function rotationAction(path, okMsg) {
  if (rotationBusy) return;
  rotationBusy = true;
  try {
    const r = await api(path, { method: "POST" });
    if (r.ok === false) toast(r.msg || "操作失败", "err");
    else if (okMsg) toast(okMsg, "ok");
    if (path.endsWith("/stop")) {
      rotationPreviewShown = false;
      rotationSummaryRun = "";
      const s = await api("/api/rotation/summary");
      renderRotationSummary(s.summary);
    }
    await loadRotation(true);
  } catch (e) {
    toast(e.message, "err");
  } finally {
    rotationBusy = false;
  }
}
$("#btn-rot-pause").addEventListener("click", () => rotationAction("/api/rotation/pause", "已暂停：当前这一轮跑完就停住"));
$("#btn-rot-resume").addEventListener("click", () => rotationAction("/api/rotation/resume", "已恢复"));
$("#btn-rot-skip").addEventListener("click", () => rotationAction("/api/rotation/skip", "已跳过当前方向，正在换下一个"));
$("#btn-rot-stop").addEventListener("click", () => rotationAction("/api/rotation/stop", "已结束，汇总见下方"));

/* ---------------- 二次炼化 ---------------- */
let refineRequest = 0;
let refineHTML = "";
let refinePage = 0;
let refineTopic = "";
let refinementLaunching = false;
let lastRefineState = {};
function paintRefinement(s) {
  lastRefineState = s;
  const busy = !!s.running || refinementLaunching;
  $("#btn-refine").disabled = busy;
  $("#btn-refine-books").disabled = busy;
  $("#btn-refine-stop").disabled = !s.running;
  const progress = $("#refine-progress");
  progress.hidden = !(s.total > 0);
  progress.value = Math.max(0, Math.min(100, 100 * (s.done || 0) / Math.max(1, s.total || 0)));
  let message = "";
  if (s.mode === "books" && s.total) {
    const counts = `已处理 ${s.done || 0}/${s.total} 条 · 跳过已完成 ${s.skipped || 0} 条 · 本次保存 ${s.saved || 0} 批`;
    if (s.running) message = `全部书籍：${counts} · 第 ${s.batch || 0}/${s.batches || 0} 批${s.current ? " · 《" + s.current + "》" : ""}${s.phase ? " · " + s.phase : ""}`;
    else if (s.stage === "stopped") message = `已停止，进度已保存；再次一键可继续。${counts}`;
    else if (s.stage === "partial") message = `本次处理完成，${s.failed || 0} 批失败；再次一键可重试未完成部分。${counts}`;
    else if (s.stage === "done") message = `全部书籍炼化完成。${counts}`;
    else message = counts;
  } else if (s.running) message = `正在炼化 ${s.done}/${s.total} 条…`;
  else if (s.stage === "stopped") message = "已停止，已保存的结果可继续查看";
  if (s.error) message = s.error + (message ? " · " + message : "");
  $("#refine-status").textContent = message;
}
async function loadRefinements() {
  const request = ++refineRequest;
  const topic = $("#kb-topic").value;
  if (topic !== refineTopic) { refinePage = 0; refineTopic = topic; }
  const page = refinePage;
  try {
    const params = new URLSearchParams({ limit: "20", offset: String(page * 20) });
    if (topic) params.set("topic", topic);
    const r = await api("/api/refinements?" + params);
    if (request !== refineRequest || topic !== $("#kb-topic").value || page !== refinePage) return;
    const s = r.state;
    paintRefinement(s);
    const total = r.total ?? (r.items || []).length;
    $("#btn-refine-prev").disabled = page === 0;
    $("#btn-refine-next").disabled = (page + 1) * 20 >= total;
    $("#refine-page").textContent = `第${page + 1}/${Math.max(1, Math.ceil(total / 20))}页 · 共${total}批结果`;
    const html = (r.items || []).map((row) => `<details class="refinement-result">
      <summary>${row.book_title ? "《" + esc(row.book_title) + "》 · " : ""}${esc(row.topic)} · ${row.input_count} 条 → ${row.items.length} 个核心 · 压缩 ${Math.round((1 - row.after_chars / Math.max(1, row.before_chars)) * 100)}% · ${new Date(row.created_at * 1000).toLocaleString()}</summary>
      ${!row.items.length ? '<p class="hint">本批核心已合并至已有炼化结果，原始出处仍可追溯。</p>' : ""}
      ${row.items.map((p) => `<div class="refinement-point"><strong>${esc(p.claim)}</strong><p>${esc(p.detail)}</p>
        ${(p.source_ids || []).map((id) => { const src = row.sources.find((s) => s.id === id); return src ? `<button class="btn sm" data-pid="${esc(id)}">原知识：${esc(src.claim).slice(0, 45)}</button>${/^https?:\/\//i.test(src.source_url || "") ? ` <a href="${esc(src.source_url)}" target="_blank" rel="noreferrer">来源原文</a>` : ""}` : ""; }).join(" ")}
      </div>`).join("")}
    </details>`).join("") || '<div class="empty">当前范围尚无炼化结果</div>';
    if (html !== refineHTML) { $("#refine-results").innerHTML = html; refineHTML = html; }
  } catch (e) { $("#refine-status").textContent = "加载失败：" + e.message; }
}
async function startRefinement(books = false) {
  if (refinementLaunching || lastRefineState.running) return;
  refinementLaunching = true;
  paintRefinement(lastRefineState);
  try {
    const r = await api(books ? "/api/refinements/books/start" : "/api/refinements/start", {
      method: "POST", ...(books ? {} : { body: { topic: $("#kb-topic").value || null } }),
    });
    if (!r.ok) toast(r.msg, "err");
    else {
      if (books) {
        $("#kb-topic").value = "";
        refinePage = 0;
        loadKnowledge();
      }
      toast(r.completed ? "全部书籍已炼化，无需重复处理" : books
        ? `已开始整理全部书籍：待处理 ${r.pending} 条，自动跳过 ${r.skipped} 条` : `已开始炼化 ${r.total} 条知识`, "ok");
    }
  } catch (e) { toast(e.message, "err"); }
  finally {
    refinementLaunching = false;
    paintRefinement(lastRefineState);
    await loadRefinements();
  }
}
$("#btn-refine").addEventListener("click", () => startRefinement());
$("#btn-refine-books").addEventListener("click", () => startRefinement(true));
$("#btn-refine-prev").addEventListener("click", () => { if (refinePage > 0) { refinePage--; loadRefinements(); } });
$("#btn-refine-next").addEventListener("click", () => { refinePage++; loadRefinements(); });
$("#btn-refine-stop").addEventListener("click", async () => {
  try { await api("/api/refinements/stop", { method: "POST" }); toast("当前模型请求完成后停止炼化"); }
  catch (e) { toast(e.message, "err"); }
});

/* ---------------- 知识库 ---------------- */
let cleanupBusy = false;
let cleanupUndo = false;
async function loadCleanupState() {
  try {
    const s = await api("/api/knowledge/cleanup");
    cleanupUndo = !!s.available;
    $("#btn-cleanup-undo").disabled = cleanupBusy || !cleanupUndo;
  } catch (_) {}
}
async function cleanupKnowledge(undo = false) {
  if (cleanupBusy) return;
  clearSimilarPreview();
  cleanupBusy = true;
  $("#btn-cleanup").disabled = true;
  $("#btn-cleanup-undo").disabled = true;
  $("#cleanup-status").textContent = undo ? "正在恢复…" : "正在扫描并清理确定重复项…";
  try {
    const result = await api(undo ? "/api/knowledge/cleanup/undo" : "/api/knowledge/cleanup", { method: "POST" });
    if (!result.ok) throw new Error(result.msg || "操作未完成");
    cleanupUndo = !!result.undo?.available;
    $("#cleanup-status").textContent = undo ? `已恢复 ${result.restored} 条知识` :
      `已清理 ${result.removed} 条确定重复项，保留 ${result.remaining} 条；近似内容保留供核对`;
    toast(undo ? "已撤销清理" : `已清理 ${result.removed} 条重复知识，可撤销`, "ok");
    state.kbVer = state.dupVer = state.anVer = -1;
    if (state.pointId) closePoint();
    await loadKnowledge();
    await refreshAll();
  } catch (e) {
    $("#cleanup-status").textContent = e.message;
    toast(e.message, "err");
  } finally {
    cleanupBusy = false;
    $("#btn-cleanup").disabled = false;
    $("#btn-cleanup-undo").disabled = !cleanupUndo;
  }
}
$("#btn-cleanup").addEventListener("click", () => cleanupKnowledge());
$("#btn-cleanup-undo").addEventListener("click", () => cleanupKnowledge(true));

async function loadKnowledge(q = state.knowledgeQuery) {
  state.knowledgeQuery = q || "";
  const request = ++state.knowledgeRequest;
  const topic = $("#kb-topic").value;
  const params = new URLSearchParams({ limit: "60" });
  if (q) params.set("q", q);
  if (topic) params.set("topic", topic);
  const url = `/api/knowledge?${params}`;
  try {
    const r = await api(url);
    if (request !== state.knowledgeRequest) return;
    renderKnowledge(r.items);
    if (r.topics) fillTopicSelect(r.topics, topic, true);
    else if (!q) fillTopicSelect(r.items, topic);
    // 记下本次渲染对应的写入版本：refreshAll 之后只在版本变化时再拉一次
    const ver = state.status && state.status.stats && state.status.stats.ver;
    if (ver !== undefined) state.kbVer = ver;
    // 近重复扫描按库版本缓存，题库没变时服务端直接命中缓存（~0ms），
    // 但仍省一次往返：版本一致就不重复问。
    if (ver !== state.dupVer || duplicateScope !== duplicateParams(duplicateThreshold()).toString()) loadDuplicates();
  } catch (e) {
    toast("知识库加载失败：" + e.message, "err");
  }
}

/* ---------------- 近似知识点：完整扫描、成对比较和确认清理 ---------------- */
let duplicateRequest = 0;
let duplicatePage = 0;
let duplicateScope = "";
let similarPreview = null;
let similarPreviewPage = 0;
let similarBusy = false;
let previewRequest = 0;

function duplicateThreshold() {
  const value = Number($("#dup-threshold").value || 90);
  if (!Number.isFinite(value) || value < 50 || value > 100) throw new Error("请输入50至100之间的相似度百分比");
  return value / 100;
}
function clearSimilarPreview() {
  previewRequest++;
  similarPreview = null;
  $("#dup-preview").hidden = true;
  $("#btn-confirm-similar").disabled = true;
}
function duplicateParams(threshold) {
  const params = new URLSearchParams({ threshold: String(threshold) });
  if ($("#kb-topic").value) params.set("topic", $("#kb-topic").value);
  return params;
}
async function loadDuplicates(threshold) {
  const request = ++duplicateRequest;
  try {
    threshold = threshold ?? duplicateThreshold();
    const params = duplicateParams(threshold);
    const scope = params.toString();
    if (scope !== duplicateScope) { duplicateScope = scope; duplicatePage = 0; }
    params.set("limit", "12");
    params.set("offset", String(duplicatePage * 12));
    const d = await api(`/api/knowledge/duplicates?${params}`);
    if (request !== duplicateRequest) return;
    const ver = state.status && state.status.stats && state.status.stats.ver;
    if (ver !== undefined) state.dupVer = ver;
    renderDuplicates(d);
  } catch (e) {
    if (request === duplicateRequest) $("#dup-status").textContent = "扫描失败：" + e.message;
  }
}
function duplicateSide(it, label = "", dropId = "") {
  return `<div class="dup-side">
    ${label ? `<span class="tagmini">${esc(label)}</span>` : ""}
    <div class="dup-claim" data-pid="${esc(it.id)}">${esc(it.claim)}</div>
    <div class="dup-meta"><span>${esc(it.topic)}</span><span>可信 ${(+it.confidence || 0).toFixed(2)}</span></div>
    <details class="dup-detail"><summary>展开完整内容与出处</summary>
      <strong>${esc(it.claim)}</strong><p>${esc(it.detail || "无补充内容")}</p>
      <div class="hint">出处：${esc(it.source_title || it.source_url || "未记录")}</div>
      <div class="hint">创建：${it.created_at ? esc(new Date(it.created_at * 1000).toLocaleString()) : "未记录"}</div>
    </details>
    ${dropId ? `<button class="btn sm dup-keep" data-drop="${esc(dropId)}" data-claim="${esc(it.claim)}">保留这条</button>` : ""}</div>`;
}
function duplicatePair(a, b, sim, preview = false) {
  const pct = Math.max(0, Math.min(100, sim * 100));
  return `<div class="dup-pair">${duplicateSide(a, preview ? "保留" : "知识点 A", preview ? "" : b.id)}
    <div class="dup-mid"><div class="dup-sim">${pct.toFixed(1)}%</div>
      <div class="dup-bar"><i style="width:${pct.toFixed(1)}%"></i></div><span class="dup-sep">相似度</span></div>
    ${duplicateSide(b, preview ? "待清除" : "知识点 B", preview ? "" : a.id)}</div>`;
}
function renderDuplicates(d) {
  const pairs = d?.pairs || [];
  const total = d?.total ?? pairs.length;
  $("#dup-card").hidden = false;
  $("#dup-threshold-label").textContent = `相似度 ≥ ${((d?.threshold ?? .9) * 100).toFixed(1)}%`;
  $("#dup-status").textContent = `已扫描 ${d?.scanned || 0} 条，发现 ${total} 条可合并的近似知识。` +
    (d?.invalid ? ` ${d.invalid} 条向量无效，已保留。` : "");
  setHTML($("#dup-body"), pairs.length ? pairs.map(p => duplicatePair(p.a, p.b, p.sim)).join("") :
    '<div class="empty">当前阈值下暂无近似知识点</div>');
  $("#btn-dup-prev").disabled = duplicatePage === 0;
  $("#btn-dup-next").disabled = (duplicatePage + 1) * 12 >= total;
  $("#dup-page").textContent = `第${duplicatePage + 1}/${Math.max(1, Math.ceil(total / 12))}页`;
}
function renderSimilarPreview() {
  if (!similarPreview) return;
  const p = similarPreview;
  $("#dup-preview").hidden = false;
  $("#dup-preview-summary").textContent = `清理预览：阈值 ${(p.threshold * 100).toFixed(1)}%，待清除 ${p.removed} 条，涉及 ${p.groups} 个保留项。以下为完整待清除列表，分页展示。`;
  $("#dup-preview-list").innerHTML = p.items.slice(similarPreviewPage * 20, (similarPreviewPage + 1) * 20)
    .map(item => `<div class="hint">${esc(item.reason)}</div>` + duplicatePair(item.keep, item.drop, item.sim, true)).join("") || '<div class="empty">没有需要清除的知识点</div>';
  $("#dup-preview-page").textContent = `第${similarPreviewPage + 1}/${Math.max(1, Math.ceil(p.items.length / 20))}页 · 共${p.removed}条`;
  $("#btn-preview-prev").disabled = similarPreviewPage === 0;
  $("#btn-preview-next").disabled = (similarPreviewPage + 1) * 20 >= p.items.length;
  $("#btn-confirm-similar").disabled = similarBusy || !p.removed;
  $("#btn-confirm-similar").textContent = `确认清除 ${p.removed} 条`;
}
async function previewSimilarKnowledge() {
  if (similarBusy || cleanupBusy) return;
  clearSimilarPreview();
  const request = previewRequest;
  similarBusy = true;
  $("#btn-cleanup-similar").disabled = true;
  $("#dup-status").textContent = "正在扫描全部知识并生成清理预览…";
  try {
    const params = duplicateParams(duplicateThreshold());
    const p = await api(`/api/knowledge/cleanup/preview?${params}`);
    if (request !== previewRequest) return;
    similarPreview = p;
    similarPreviewPage = 0;
    similarBusy = false;
    renderSimilarPreview();
    $("#dup-status").textContent = "预览完成，尚未删除任何知识点。";
  } catch (e) { $("#dup-status").textContent = e.message; toast(e.message, "err"); }
  finally { similarBusy = false; $("#btn-cleanup-similar").disabled = false; }
}
async function confirmSimilarCleanup() {
  if (!similarPreview || similarBusy || cleanupBusy) return;
  const plan = similarPreview;
  similarBusy = cleanupBusy = true;
  $("#btn-confirm-similar").disabled = true;
  $("#btn-cleanup-similar").disabled = true;
  $("#btn-cleanup").disabled = $("#btn-cleanup-undo").disabled = true;
  try {
    const result = await api("/api/knowledge/cleanup", { method:"POST", body:{ threshold:plan.threshold, topic:plan.topic, token:plan.token } });
    if (!result.ok) throw new Error(result.msg || "清除失败");
    clearSimilarPreview();
    cleanupUndo = !!result.undo?.available;
    $("#cleanup-status").textContent = `已归档 ${result.removed} 条高相似度知识，可撤销。`;
    toast(`已清除 ${result.removed} 条近似知识，可撤销`, "ok");
    state.kbVer = state.dupVer = state.anVer = -1;
    if (state.pointId) closePoint();
    await loadKnowledge();
    await refreshAll();
  } catch(e) { clearSimilarPreview(); $("#dup-status").textContent = e.message; toast(e.message, "err"); }
  finally {
    similarBusy = cleanupBusy = false;
    $("#btn-cleanup-similar").disabled = $("#btn-cleanup").disabled = false;
    $("#btn-cleanup-undo").disabled = !cleanupUndo;
  }
}
$("#dup-threshold").addEventListener("input", () => { clearSimilarPreview(); state.dupVer = -1; loadDuplicates(); });
$("#btn-cleanup-similar").addEventListener("click", previewSimilarKnowledge);
$("#btn-confirm-similar").addEventListener("click", confirmSimilarCleanup);
$("#btn-cancel-similar").addEventListener("click", clearSimilarPreview);
$("#btn-preview-prev").addEventListener("click", () => { if (similarPreviewPage > 0) { similarPreviewPage--; renderSimilarPreview(); } });
$("#btn-preview-next").addEventListener("click", () => { if (similarPreview && (similarPreviewPage + 1) * 20 < similarPreview.items.length) { similarPreviewPage++; renderSimilarPreview(); } });
$("#btn-dup-prev").addEventListener("click", () => { if (duplicatePage > 0) { duplicatePage--; loadDuplicates(); } });
$("#btn-dup-next").addEventListener("click", () => { duplicatePage++; loadDuplicates(); });

function fillTopicSelect(items, activeTopic = "", complete = false) {
  const sel = $("#kb-topic");
  const topics = [...new Set(items.map((i) => i.topic))];
  if (complete && activeTopic && !topics.includes(activeTopic)) topics.push(activeTopic);
  if (!topics.length) return;
  const html =
    '<option value="">全部方向</option>' +
    topics.map((t) => `<option value="${esc(t)}">${esc(t)}</option>`).join("");
  if (sel.dataset.filled === "1") {
    // 已填过：仅当方向集合真的变了才重建（保留用户当前筛选）。
    // 筛选态下 items 只有该方向一个主题，绝不能拿来重建选项，否则
    // 下拉里就只剩当前方向、切不回去了。
    if ((!complete && activeTopic) || sel.innerHTML === html) return;
    sel.innerHTML = html;
    if (complete) sel.value = activeTopic;
    return;
  }
  sel.innerHTML = html;
  if (complete) sel.value = activeTopic;
  sel.dataset.filled = "1";
}

function renderKnowledge(items) {
  const box = $("#kb-list");
  if (!items || !items.length) {
    box.innerHTML = '<div class="empty"><div class="big">▤</div>暂无知识点</div>';
    return;
  }
  box.innerHTML = items
    .map((p) => {
      const conf = Math.round((p.confidence || 0) * 100);
      const tags = (p.tags || []).map((t) => `<span class="tagmini">${esc(t)}</span>`).join(" ");
      const src = p.source_url
        ? `<a href="${esc(p.source_url)}" target="_blank" rel="noreferrer">${esc(
            p.source_title || p.source_url
          ).slice(0, 70)}</a>`
        : "";
      return `<div class="row" data-pid="${esc(p.id || "")}" title="点击查看详情与相似知识点">
        <div class="claim">${esc(p.claim)}</div>
        ${p.detail ? `<div class="detail">${esc(p.detail)}</div>` : ""}
        <div class="meta">
          <span class="tagmini">${esc(p.topic)}</span>
          ${tags}
          <span>可信度 <span class="conf-bar"><i style="width:${conf}%"></i></span></span>
          ${src}
          <span style="margin-left:auto">${fmtTime(p.created_at)}</span>
        </div>
      </div>`;
    })
    .join("");
}

/* ---------------- 知识点详情抽屉 ---------------- */
/* 点任一条知识点，右侧滑出详情：全文 + 出处 + 与它语义最近的邻居。
   邻居来自后端 similar_points（点积排序，不再走一次 LLM 嵌入），
   相似度条把「这条和哪条最像」直接画出来——这是判断一条结论可不可靠
   最省事的办法：如果最近邻都跟它说的不是一回事，说明它可能是孤证。 */
async function openPoint(pid) {
  if (!pid) return;
  const mask = $("#pt-mask");
  const drawer = $("#pt-drawer");
  if (!mask || !drawer) return;
  state.pointId = pid;
  mask.hidden = false;
  drawer.hidden = false;
  setHTML($("#pt-body"), '<div class="hint" style="padding:8px 2px">加载中…</div>');
  let d;
  try {
    d = await api(`/api/knowledge/${encodeURIComponent(pid)}?k=5`);
  } catch (e) {
    setHTML($("#pt-body"), `<div class="empty">加载失败：${esc(e.message)}</div>`);
    return;
  }
  if (state.pointId !== pid) return; // 期间又点了别的，丢弃这次结果
  renderPoint(d);
}

function closePoint() {
  state.pointId = null;
  const mask = $("#pt-mask");
  const drawer = $("#pt-drawer");
  if (mask) mask.hidden = true;
  if (drawer) drawer.hidden = true;
  // 从深链进来时把地址退回列表，避免刷新又弹一次
  if (String(location.hash).startsWith("#knowledge/")) location.hash = "knowledge";
}

function renderPoint(d) {
  const p = d.item || {};
  const sims = d.similar || [];
  const conf = Math.round((p.confidence || 0) * 100);
  const tags = (p.tags || []).map((t) => `<span class="tagmini">${esc(t)}</span>`).join(" ");
  const src = p.source_url
    ? `<a href="${esc(p.source_url)}" target="_blank" rel="noreferrer">${esc(
        p.source_title || p.source_url
      )}</a>`
    : '<span class="dim">没有记录出处</span>';

  const simHtml = sims.length
    ? sims
        .map((s) => {
          const pct = Math.max(0, Math.min(100, (s.similarity || 0) * 100)).toFixed(1);
          return `<div class="sim-item" data-pid="${esc(s.id || "")}" title="点击查看这条">
            <div class="sim-claim">${esc(s.claim)}</div>
            <div class="sim-meta">
              <span class="tagmini">${esc(s.topic)}</span>
              <span class="mono dim">相似 ${(+s.similarity || 0).toFixed(3)}</span>
            </div>
            <div class="sim-bar"><i style="width:${pct}%"></i></div>
          </div>`;
        })
        .join("")
    : `<div class="empty" style="text-align:left;line-height:1.8;padding:2px 2px 6px">
        <div style="font-weight:600;margin-bottom:4px">这条还比较孤立</div>
        <div>库里暂时没有语义相近的知识点。同方向再多学几轮、或补几份不同视角的资料，这里就会出现互为参照的邻居——它们是检验一条结论是否可靠最省事的材料。</div>
        <button class="btn primary sm" data-goto="learn" style="margin-top:10px">去学同方向</button>
      </div>`;

  setHTML(
    $("#pt-body"),
    `<div class="pt-topic">${esc(p.topic || "")}</div>
     <div class="pt-claim">${esc(p.claim || "")}</div>
     ${p.detail ? `<div class="pt-detail">${esc(p.detail)}</div>` : ""}
     <div class="pt-meta">
       <span>可信度 <span class="conf-bar"><i style="width:${conf}%"></i></span> <span class="mono dim">${(+(p.confidence || 0)).toFixed(2)}</span></span>
       ${tags}
       <span class="dim">${fmtTime(p.created_at)}</span>
     </div>
     <div class="pt-src"><span class="dim">出处</span> ${src}</div>
     ${p.archived ? `<div class="hint">这条知识已归档为重复项，历史出处仍保留。<button class="btn sm" data-pid="${esc(p.canonical_id)}">查看保留条目</button></div>` : ""}
     ${(p.duplicate_sources || []).length ? `<div class="hint">已合并的其他出处：${p.duplicate_sources.map((s) => `<button class="btn sm" data-pid="${esc(s.id)}">${esc(s.source_title || "查看原知识")}</button>`).join(" ")}</div>` : ""}
     ${(p.merged_sources || []).length ? `<details class="dup-detail"><summary>入库查重合并的原始内容与出处（${p.merged_sources.length}条）</summary>${p.merged_sources.map(s => `<div><strong>${esc(s.claim)}</strong><p>${esc(s.detail)}</p><span class="hint">${esc(s.source_title || s.source_url)}</span></div>`).join("")}</details>` : ""}
     <div class="pt-actions">
       <button class="btn sm" data-goto="knowledge" data-topic="${esc(p.topic || "")}">只看这个方向</button>
       <button class="btn sm pt-ask" data-claim="${esc((p.claim || "").slice(0, 120))}">就这条提问</button>
       <button class="btn sm pt-copy">复制链接</button>
     </div>
     <div class="pt-sim-head">
       <span>语义最近的 ${sims.length} 条</span>
       <span class="hint" style="margin:0">向量余弦，越高越接近</span>
     </div>
     <div class="pt-sims">${simHtml}</div>`
  );
}

$("#pt-close").addEventListener("click", closePoint);
$("#pt-mask").addEventListener("click", closePoint);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.pointId) closePoint();
});

$("#btn-kb-search").addEventListener("click", () => {
  const q = $("#kb-search").value.trim();
  loadKnowledge(q);
});
$("#kb-search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("#btn-kb-search").click();
});
$("#kb-topic").addEventListener("change", () => { clearSimilarPreview(); state.dupVer = -1; loadKnowledge(); loadRefinements(); });

/* 分析页手动刷新：绕过版本门控强制重算+重绘（服务端也会强制重聚） */
$("#btn-an-refresh").addEventListener("click", () => loadAnalytics(true));

/* ---------------- 书库 ---------------- */
let libSelected = new Set();
let libPoll = null;

const BOOK_STATUS = {
  pending: ["待解析", "var(--text-dim)"],
  parsing: ["解析中", "var(--accent)"],
  ready: ["可导入", "var(--green)"],
  importing: ["导入中", "var(--accent)"],
  done: ["已导入", "var(--green)"],
  skipped: ["已跳过", "var(--amber)"],
  failed: ["失败", "var(--red)"],
};

async function loadLibrary() {
  try {
    const [st, dirs] = await Promise.all([
      api("/api/library/stats"),
      api("/api/library/dirs"),
    ]);
    const s = st.stats;
    $("#lib-dirs").textContent = s.dirs;
    $("#lib-books").textContent = s.books;
    $("#lib-pending").textContent = `待解析 ${s.pending}`;
    $("#lib-done").textContent = s.done;
    $("#lib-skipped").textContent = `跳过 ${s.skipped}`;
    $("#lib-points").textContent = s.points;
    renderLibDirs(dirs);
    await loadLibBooks();
    paintLibTask(st.task);
    // 刷新页面 / 切回书库时，正在跑的 OCR 进度要能接回来，否则看起来像「没反应」
    if (!st.task || !st.task.running) {
      const o = await api("/api/ocr/state").catch(() => null);
      if (o && (o.batch || o.running)) paintOcrLive(o);
    }
  } catch (e) {
    toast("书库加载失败：" + e.message, "err");
  }
}

function renderLibDirs(dirs) {
  const box = $("#lib-dir-list");
  state.libDirs = dirs;
  // 选中的目录如果被删掉了，下钻范围要一起失效，否则会筛出一个空列表
  if (state.libDir && !dirs.some((d) => d.id === state.libDir)) state.libDir = null;
  if (!dirs.length) {
    box.innerHTML = '<div class="empty">还没有添加目录</div>';
    renderLibDirSummary([]);
    return;
  }
  box.innerHTML = dirs
    .map((d) => {
      const total = d.book_count || 0;
      const done = d.imported_count || 0;
      const pct = total ? Math.round((done / total) * 100) : 0;
      // 三种状态分开说：空目录要引导去扫描；有剩的要指出还差多少本
      let chip, chipCls;
      if (!total) {
        chip = "未扫描";
        chipCls = "amber";
      } else if (done >= total) {
        chip = "已全部导入";
        chipCls = "green";
      } else {
        chip = `待导入 ${total - done} 本`;
        chipCls = "amber";
      }
      const active = state.libDir === d.id;
      return `<div class="row lib-dir-row${active ? " active" : ""}" data-dir-open="${esc(d.id)}">
        <div class="rowflex" style="justify-content:space-between;gap:8px">
          <span style="font-size:13.5px;font-weight:600;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(d.label || d.path)}</span>
          <div class="rowflex" style="gap:6px;flex-shrink:0">
            <span class="chipmini ${chipCls}">${chip}</span>
            <button class="btn sm" data-dir-scan="${esc(d.id)}">扫描</button>
            <button class="btn sm danger" data-dir-del="${esc(d.id)}">移除</button>
          </div>
        </div>
        <div class="meta dir-meta">
          <span class="mono dir-path" title="${esc(d.path)}">${esc(d.path)}</span>
          <span class="dirbar" title="${done} / ${total} 本已导入">
            <i style="width:${pct}%" class="${pct >= 100 ? "full" : ""}"></i>
          </span>
          <span class="dir-count">${done} / ${total} 本 · ${pct}%</span>
          ${(d.excludes || []).length ? `<span class="tagmini">排除 ${esc(d.excludes.join("、"))}</span>` : ""}
        </div>
      </div>`;
    })
    .join("");
  renderLibDirSummary(dirs);

  // 点目录行 = 下钻到该目录的书目（再点一次取消）
  $$("#lib-dir-list [data-dir-open]").forEach((row) =>
    row.addEventListener("click", (e) => {
      if (e.target.closest("button")) return; // 扫描/移除按钮不触发下钻
      const id = row.dataset.dirOpen;
      state.libDir = state.libDir === id ? null : id;
      renderLibDirs(dirs);
      loadLibBooks();
    })
  );

  $$("#lib-dir-list [data-dir-scan]").forEach((b) =>
    b.addEventListener("click", () => doScan(b.dataset.dirScan))
  );
  $$("#lib-dir-list [data-dir-del]").forEach((b) =>
    b.addEventListener("click", async () => {
      if (!confirm("移除这个目录？（已登记的书目记录会保留）")) return;
      await api("/api/library/dirs/" + b.dataset.dirDel, { method: "DELETE" });
      toast("已移除");
      loadLibrary();
    })
  );
}

/** 目录总览：把「多少目录 / 多少本 / 导入到哪一步」压成一句话 + 一条总进度。 */
function renderLibDirSummary(dirs) {
  const box = $("#lib-dir-summary");
  if (!box) return;
  const books = dirs.reduce((a, d) => a + (d.book_count || 0), 0);
  const done = dirs.reduce((a, d) => a + (d.imported_count || 0), 0);
  const empty = dirs.filter((d) => !(d.book_count || 0)).length;
  if (!dirs.length) {
    box.innerHTML =
      '<span class="empty" style="padding:0">添加一个书库目录，点「扫描」就能发现里面的书。</span>';
    return;
  }
  const pct = books ? Math.round((done / books) * 100) : 0;
  const hint = empty
    ? `${empty} 个目录还没扫描过，点它的「扫描」按钮开始`
    : done < books
      ? `还有 ${books - done} 本没导入，点目录卡筛出来批量导入`
      : "所有目录都已导入完成";
  box.innerHTML =
    `<div class="dirsum-head">
       <span class="dirsum-line">${dirs.length} 个目录 · ${books} 本 · 已导入 ${done} 本</span>
       <span class="dirsum-pct ${pct >= 100 ? "full" : ""}">${pct}%</span>
     </div>` +
    `<div class="dirbar"><i style="width:${pct}%" class="${pct >= 100 ? "full" : ""}"></i></div>` +
    `<div class="dirsum-hint">${esc(hint)}</div>`;
}

async function loadLibBooks() {
  const status = $("#lib-filter").value;
  const q = $("#lib-search").value.trim();
  // 列表内容变了，之前勾选的书可能已经不在列表里。
  // 不清空的话，点「导入选中」会导入用户看不见的书。
  if (libSelected.size) {
    libSelected.clear();
    updateLibImportBtn();
  }
  // 后端 list_books 的上限是 1000，这里取满，否则「全选」永远选不全
  const params = new URLSearchParams({ limit: "1000" });
  if (status) params.set("status", status);
  if (q) params.set("q", q);
  if (state.libDir) params.set("dir_id", state.libDir);
  try {
    const r = await api("/api/library/books?" + params);
    renderLibBooks(r.items, r.total);
    renderLibScope(r.total);
  } catch (e) {
    toast("书目加载失败：" + e.message, "err");
  }
}

/** 书目区顶部的「当前范围」提示条：让用户随时知道自己看的是哪个目录的子集。 */
function renderLibScope(total) {
  const bar = $("#lib-scope");
  if (!bar) return;
  if (!state.libDir) {
    bar.style.display = "none";
    bar.innerHTML = "";
    return;
  }
  const d = (state.libDirs || []).find((x) => x.id === state.libDir);
  const label = d ? d.label || d.path : state.libDir;
  bar.style.display = "";
  bar.innerHTML =
    `<span class="scope-chip">只看：${esc(label)}</span>` +
    `<span class="scope-n">${total} 本</span>` +
    `<button class="btn sm" id="lib-scope-clear">显示全部目录</button>`;
  const clr = $("#lib-scope-clear");
  if (clr) {
    clr.addEventListener("click", () => {
      state.libDir = null;
      renderLibDirs(state.libDirs || []);
      loadLibBooks();
    });
  }
}

function renderLibBooks(items, total) {
  const box = $("#lib-book-list");
  $("#lib-total").textContent = total;
  window.__libBooks = items;
  if (!items.length) {
    box.innerHTML = '<div class="empty">没有匹配的书目</div>';
    return;
  }
  box.innerHTML = items
    .map((b) => {
      const [txt, color] = BOOK_STATUS[b.status] || [b.status, "var(--text-dim)"];
      // skipped（扫描版）也要能选：全选后可以批量 OCR
      // done 也放开勾选：导入中途被打断的书恢复成 done 后，得能继续导入；
      // 重新导入有语义去重兜底，不会堆重复知识点
      const canPick = ["pending", "ready", "skipped", "done"].includes(b.status);
      const size = b.size_bytes
        ? (b.size_bytes / 1024 / 1024).toFixed(1) + "MB"
        : "";
      // 扫描版（被跳过且原因是扫描）或已有 OCR 结果的，给个 OCR 入口
      const needOcr =
        b.format === "pdf" &&
        (b.status === "skipped" || b.status === "ready" || b.status === "done");
      return `<div class="row" style="display:flex;gap:11px;align-items:flex-start">
        <input type="checkbox" class="lib-ck" data-id="${b.id}"
               ${canPick ? "" : "disabled"}
               ${libSelected.has(b.id) ? "checked" : ""}
               style="width:15px;height:15px;margin-top:3px;flex-shrink:0;cursor:pointer" />
        <div style="flex:1;min-width:0">
          <div class="rowflex" style="justify-content:space-between;gap:10px">
            <span style="font-size:13.3px">${esc(b.title || b.path)}</span>
            <span class="rowflex" style="gap:7px;white-space:nowrap">
              <span style="color:${color};font-size:11.5px">${txt}</span>
              ${needOcr ? `<button class="btn sm" data-ocr="${b.id}">OCR</button>` : ""}
            </span>
          </div>
          <div class="meta">
            <span class="tagmini">${esc(b.format)}</span>
            ${size ? `<span>${size}</span>` : ""}
            ${b.pages ? `<span>${b.pages} 页</span>` : ""}
            ${b.chars ? `<span>${b.chars} 字</span>` : ""}
            ${b.points_count ? `<span style="color:var(--green)">${b.points_count} 条知识</span>` : ""}
            ${b.topic ? `<span class="tagmini">${esc(b.topic).slice(0, 24)}</span>` : ""}
          </div>
          ${b.skip_reason ? `<div class="detail" style="color:var(--amber)">${esc(b.skip_reason)}</div>` : ""}
          ${b.error ? `<div class="detail" style="color:var(--red)">${esc(b.error)}</div>` : ""}
        </div>
      </div>`;
    })
    .join("");

  $$("#lib-book-list .lib-ck").forEach((ck) =>
    ck.addEventListener("change", () => {
      if (ck.checked) libSelected.add(ck.dataset.id);
      else libSelected.delete(ck.dataset.id);
      updateLibImportBtn();
    })
  );
  $$("#lib-book-list [data-ocr]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const book = (window.__libBooks || []).find((x) => x.id === btn.dataset.ocr);
      if (book) openOcr(book);
    })
  );
  updateLibImportBtn();
}

function updateLibImportBtn() {
  const n = libSelected.size;
  const btn = $("#btn-lib-import");
  if (btn) {
    btn.textContent = `导入选中（${n}）`;
    btn.disabled = n === 0;
  }
  const pb = $("#btn-lib-parse");
  if (pb) {
    pb.textContent = `解析选中（${n}）`;
    pb.disabled = n === 0;
  }
  const ob = $("#btn-lib-ocr");
  if (ob) {
    ob.textContent = `OCR 选中（${n}）`;
    ob.disabled = n === 0;
  }
}

function paintLibTask(t) {
  const running = t && t.running;
  $("#btn-lib-scan").disabled = running;
  $("#btn-lib-stop").disabled = !running;
  $("#btn-lib-url").disabled = running;
  const wrap = $("#lib-bar-wrap");
  if (running) {
    wrap.style.display = "block";
    const pct = t.total ? Math.round((t.done / t.total) * 100) : 0;
    $("#lib-bar").style.width = pct + "%";
    const stage = t.kind === "scan" ? "扫描中" : "导入中";
    $("#lib-task-hint").textContent = `${stage} ${t.done}/${t.total}${t.current ? " · " + t.current.slice(0, 40) : ""}${t.points ? " · 已入库 " + t.points + " 条" : ""}`;
    if (!libPoll) libPoll = setInterval(pollLibTask, 2500);
  } else {
    wrap.style.display = "none";
    if (libPoll) {
      clearInterval(libPoll);
      libPoll = null;
    }
  }
}

async function pollLibTask() {
  try {
    // OCR 任务和书库任务是两套状态，批量 OCR 时 library/state 是空的，
    // 只查它的话书库页会完全没反应
    const [t, o] = await Promise.all([
      api("/api/library/state").catch(() => ({})),
      api("/api/ocr/state").catch(() => null),
    ]);
    if (o && (o.batch || o.running)) {
      paintOcrLive(o);
    } else {
      paintLibTask(t);
    }
    if (!t.running && !(o && (o.batch || o.running))) {
      loadLibrary();
    }
  } catch (_) {}
}

// 批量 OCR 的进度：外层是第几本，内层是当前书第几页
function paintOcrLive(o) {
  $("#btn-lib-scan").disabled = true;
  $("#btn-lib-stop").disabled = false;
  $("#btn-lib-url").disabled = true;
  const wrap = $("#lib-bar-wrap");
  if (!wrap) return;
  wrap.style.display = "block";
  const total = o.batch_total || 0;
  const doneBooks = o.batch_done || 0;
  const cur = o.total ? o.done / o.total : 0;
  // 批量：总体进度 = 已完成的书 + 当前这本的页进度；单本：就是页进度
  const overall = o.batch
    ? (total ? Math.round(((doneBooks + (o.running ? cur : 0)) / total) * 100) : 0)
    : Math.round(cur * 100);
  const bar = $("#lib-bar");
  if (bar) bar.style.width = overall + "%";
  const hint = $("#lib-task-hint");
  if (hint) {
    const idx = total ? Math.min(Math.max(doneBooks + (o.running ? 1 : 0), 1), total) : 0;
    const head = o.batch ? `OCR 批量 ${idx}/${total} 本 · 总体 ${overall}%` : `OCR 识别中 · ${overall}%`;
    const inner = o.running && o.total ? ` · 当前书 ${o.done}/${o.total} 页` : " · 正在切下一本";
    const name = o.book_title ? ` · ${o.book_title.slice(0, 26)}` : "";
    hint.innerHTML =
      `${head}${inner}${name}` +
      ` <a href="#" id="lib-ocr-peek" style="color:var(--accent)">查看</a>`;
    const peek = $("#lib-ocr-peek");
    if (peek) {
      peek.addEventListener("click", (ev) => {
        ev.preventDefault();
        // follow=true：之后每换一本书，抽屉自动跟着切
        if (o.book_id) openOcr({ id: o.book_id, title: o.book_title || "" }, true);
      });
    }
  }
  if (!libPoll) libPoll = setInterval(pollLibTask, 2000);
}

async function doScan(dirId) {
  toast("开始扫描…");
  try {
    const r = await api("/api/library/scan", {
      method: "POST",
      body: dirId ? { dirs: [dirId] } : {},
    });
    if (r.ok) toast("已开始后台扫描，可在书库查看进度", "ok");
    else toast(r.msg || "扫描未启动", "err");
    loadLibrary();
  } catch (e) {
    toast("扫描失败：" + e.message, "err");
  }
}

$("#btn-lib-add").addEventListener("click", async () => {
  const path = $("#lib-path").value.trim();
  if (!path) return toast("填一个目录路径", "err");
  const ex = $("#lib-excludes")
    .value.split(/[,，]/)
    .map((s) => s.trim())
    .filter(Boolean);
  try {
    const r = await api("/api/library/dirs", {
      method: "POST",
      body: { path, excludes: ex },
    });
    if (!r.ok) return toast(r.msg || "添加失败", "err");
    toast("目录已添加", "ok");
    $("#lib-path").value = "";
    loadLibrary();
  } catch (e) {
    toast(e.message, "err");
  }
});

$("#btn-lib-scan").addEventListener("click", () => doScan(null));

// 新增元素一律做存在性检查：浏览器缓存了旧 HTML 时，找不到元素直接跳过，
// 不要让一个 null.addEventListener 把整个初始化中断掉
const _btnParse = $("#btn-lib-parse");
if (_btnParse) _btnParse.addEventListener("click", async () => {
  const ids = Array.from(libSelected);
  if (!ids.length) return toast("先勾选要解析的书", "err");
  const ok = confirm(
    `对选中的 ${ids.length} 本书做解析？\n\n` +
      "解析只提取正文、判断有没有文字层，不调用大模型，每本几秒钟。\n" +
      "结果：有文字层的变成「可导入」，扫描版会被单独标出来。\n\n" +
      "（要真正进知识库还得再点「导入选中」，那个慢得多，每本十几分钟）"
  );
  if (!ok) return;
  try {
    const r = await api("/api/library/parse", {
      method: "POST",
      body: { book_ids: ids },
    });
    if (!r.ok) return toast(r.msg || "启动失败", "err");
    toast(r.msg || "已开始解析", "ok");
    libSelected.clear();
    updateLibImportBtn();
    pollLibTask();
  } catch (e) {
    toast(e.message, "err");
  }
});

$("#btn-lib-import").addEventListener("click", async () => {
  const ids = Array.from(libSelected);
  if (!ids.length) return toast("先勾选要导入的书", "err");
  try {
    const r = await api("/api/library/import", {
      method: "POST",
      body: { book_ids: ids, topic: $("#lib-topic").value.trim() },
    });
    if (!r.ok) return toast(r.msg || "启动失败", "err");
    toast(`已开始导入 ${ids.length} 本，后台运行`, "ok");
    libSelected.clear();
    updateLibImportBtn();
    pollLibTask();
  } catch (e) {
    toast(e.message, "err");
  }
});

$("#btn-lib-stop").addEventListener("click", async () => {
  await api("/api/library/stop", { method: "POST" });
  toast("已发送停止指令");
});

$("#btn-lib-url").addEventListener("click", async () => {
  const url = $("#lib-url").value.trim();
  if (!url) return toast("粘贴一个链接", "err");
  toast("正在获取链接内容…");
  try {
    const r = await api("/api/library/import-url", {
      method: "POST",
      body: { url, topic: $("#lib-topic").value.trim() },
    });
    toast(r.ok ? `导入完成：${r.points || 0} 条知识` : r.msg || "失败", r.ok ? "ok" : "err");
    if (r.ok) $("#lib-url").value = "";
    loadLibrary();
  } catch (e) {
    toast(e.message, "err");
  }
});

$("#lib-filter").addEventListener("change", loadLibBooks);
$("#lib-search").addEventListener("input", () => {
  clearTimeout(window.__libT);
  window.__libT = setTimeout(loadLibBooks, 400);
});

// 按当前筛选条件分页拉取全部 id —— 「全选」要选真的全部，
// 而不是当前这一页（后端单次上限 1000）
async function fetchAllIds(maxCount = 0) {
  const status = $("#lib-filter").value;
  const q = $("#lib-search").value.trim();
  const ids = [];
  let offset = 0;
  for (let guard = 0; guard < 50; guard++) {
    // 只要前 N 个时按需取，别把三千多个 id 全拉一遍
    const remain = maxCount ? maxCount - ids.length : 1000;
    const pageSize = Math.max(1, Math.min(1000, remain));
    const params = new URLSearchParams({ limit: String(pageSize), offset: String(offset) });
    if (status) params.set("status", status);
    if (q) params.set("q", q);
    const r = await api(`/api/library/books?${params}`);
    const items = r.items || [];
    for (const b of items) ids.push(b.id);
    if (items.length < pageSize) break;
    if (maxCount && ids.length >= maxCount) break;
    offset += pageSize;
  }
  return ids;
}

function syncLibCheckboxes() {
  $$("#lib-book-list .lib-ck").forEach((ck) => {
    ck.checked = libSelected.has(ck.dataset.id);
  });
}

async function selectAllMatching(limitN) {
  const btn = $("#btn-lib-selectall");
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "拉取中…";
  try {
    let ids = await fetchAllIds(limitN || 0);
    if (limitN) ids = ids.slice(0, limitN);
    libSelected = new Set(ids);
    syncLibCheckboxes();
    updateLibImportBtn();
    toast(`已选中 ${ids.length} 本`);
  } catch (e) {
    toast("拉取失败：" + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

const _btnSelAll = $("#btn-lib-selectall");
if (_btnSelAll) _btnSelAll.addEventListener("click", () => selectAllMatching(0));

const _btnSelN = $("#btn-lib-selectn");
if (_btnSelN) _btnSelN.addEventListener("click", () => {
  const n = parseInt($("#lib-select-n").value, 10);
  if (!n || n < 1) return toast("先填一个正整数", "err");
  selectAllMatching(n);
});

const _btnUnsel = $("#btn-lib-unselect");
if (_btnUnsel) _btnUnsel.addEventListener("click", () => {
  libSelected.clear();
  syncLibCheckboxes();
  updateLibImportBtn();
});

const _btnOcr = $("#btn-lib-ocr");
if (_btnOcr) _btnOcr.addEventListener("click", async () => {
  const ids = Array.from(libSelected);
  if (!ids.length) return toast("先勾选要识别的书", "err");
  const ok = confirm(
    `对选中的 ${ids.length} 本书开始 OCR？\n\n` +
      "默认识别全部页面、快速引擎（约 5 秒/页）。\n" +
      "大书会跑很久，可以随时点「停止」。\n\n" +
      "建议先用 1 本试，确认识别质量再批量跑。"
  );
  if (!ok) return;
  try {
    const r = await api("/api/ocr/batch", {
      method: "POST",
      body: { book_ids: ids, pages: "all", engine: "rapid" },
    });
    if (!r.ok) return toast(r.msg || "启动失败", "err");
    toast(r.msg || `已开始识别 ${ids.length} 本`, "ok");
    libSelected.clear();
    updateLibImportBtn();
    // 立刻接上进度轮询：以前这里只延迟刷一次列表，所以批量跑起来完全看不到进度
    pollLibTask();
    setTimeout(() => loadLibBooks().catch(() => {}), 6000);
  } catch (e) {
    toast(e.message, "err");
  }
});

/* ---------------- OCR 识别与校对 ---------------- */
let ocrBook = null;
let ocrPageNo = 0;
let ocrPoll = null;
// 批量识别时，抽屉自动跟着「正在识别的那本书」走
let ocrFollow = false;
// 自动预览当前正在识别的页；用户手动点过页就关掉，避免抢走他的选择
let ocrAutoPage = true;

function openOcr(book, follow = false) {
  ocrBook = book;
  ocrPageNo = 0;
  ocrFollow = follow;
  ocrAutoPage = true;
  $("#ocr-title").textContent = book.title || book.path || "—";
  $("#ocr-sub").textContent = `${(book.format || "").toUpperCase()}${book.pages ? " · " + book.pages + " 页" : ""}`;
  $("#ocr-text").value = "";
  $("#ocr-page-info").textContent = "";
  $("#ocr-img").removeAttribute("src");
  $("#ocr-page-list").innerHTML = "";
  $("#ocr-drawer").classList.add("open");
  loadOcrPages();
  paintOcrDevice();
  pollOcr();
}

// 让用户看得见识别到底跑在 GPU 还是 CPU 上
async function paintOcrDevice() {
  const el = $("#ocr-device");
  if (!el) return;
  try {
    const d = await api("/api/ocr/device");
    const gpu = d.active === "gpu";
    el.textContent = gpu ? `GPU · ${d.workers} 线程` : `CPU · ${d.workers} 线程`;
    el.style.color = gpu ? "var(--green)" : "var(--text-dim)";
    el.title = gpu
      ? "识别走 CUDA，页面渲染与识别并行"
      : d.cuda_available
        ? "已检测到 CUDA，但配置里关掉了（ocr_device=cpu）"
        : "未检测到 CUDA：装 onnxruntime-gpu 即可用显卡加速";
  } catch (_) {
    el.textContent = "";
  }
}

// 批量识别跑到下一本时，把抽屉整个重挂到新书上
function attachOcrBook(s) {
  ocrBook = { id: s.book_id, title: s.book_title || "", format: "", pages: s.total || 0 };
  ocrPageNo = -1;
  ocrAutoPage = true;
  $("#ocr-title").textContent = ocrBook.title || "（正在准备）";
  $("#ocr-sub").textContent = "批量识别中…";
  $("#ocr-text").value = "";
  $("#ocr-page-info").textContent = "";
  $("#ocr-img").removeAttribute("src");
  $("#ocr-page-list").innerHTML = "";
  loadOcrPages();
}

function closeOcr() {
  $("#ocr-drawer").classList.remove("open");
  ocrBook = null;
  ocrFollow = false;
  if (ocrPoll) {
    clearInterval(ocrPoll);
    ocrPoll = null;
  }
}

async function loadOcrPages() {
  if (!ocrBook) return;
  try {
    const r = await api(`/api/library/books/${ocrBook.id}/ocr`);
    const st = r.stats || { pages: 0, chars: 0 };
    $("#ocr-sub").textContent =
      `${(ocrBook.format || "").toUpperCase()} · ${ocrBook.pages || "?"} 页 · 已识别 ${st.pages} 页 / ${st.chars} 字`;
    const box = $("#ocr-page-list");
    if (!r.pages.length) {
      box.innerHTML = '<div class="empty" style="padding:14px">还没有识别任何页</div>';
      return;
    }
    box.innerHTML = r.pages
      .map(
        (p) => `<div class="pg ${p.page_no === ocrPageNo ? "active" : ""}" data-pg="${p.page_no}">
          <span>第 ${p.page_no + 1} 页${p.edited ? " ✓" : ""}</span>
          <span class="n">${p.chars}</span>
        </div>`
      )
      .join("");
    $$("#ocr-page-list .pg").forEach((el) =>
      el.addEventListener("click", () => selectOcrPage(+el.dataset.pg))
    );
  } catch (e) {
    toast("加载识别记录失败：" + e.message, "err");
  }
}

async function selectOcrPage(pno, auto = false) {
  if (!ocrBook) return;
  if (!auto) ocrAutoPage = false;
  ocrPageNo = pno;
  $$("#ocr-page-list .pg").forEach((el) =>
    el.classList.toggle("active", +el.dataset.pg === pno)
  );
  $("#ocr-img").src = `/api/library/books/${ocrBook.id}/page/${pno}.png?dpi=120`;
  $("#ocr-page-info").textContent = "加载中…";
  try {
    const d = await api(`/api/library/books/${ocrBook.id}/ocr/${pno}`);
    // 自动预览时别覆盖用户正在编辑的文本框
    if (!auto || document.activeElement !== $("#ocr-text")) {
      $("#ocr-text").value = d.text || "";
    }
    $("#ocr-page-info").textContent =
      `${d.chars} 字 · ${d.engine}${d.elapsed ? " · " + Math.round(d.elapsed * 10) / 10 + "s" : ""}${d.edited ? " · 已校对" : ""}`;
  } catch (_) {
    $("#ocr-text").value = "";
    $("#ocr-page-info").textContent = "这一页还没识别";
  }
}

async function startOcr() {
  if (!ocrBook) return;
  const pages = $("#ocr-pages").value.trim() || "all";
  const engine = $("#ocr-engine").value;
  try {
    const r = await api(`/api/library/books/${ocrBook.id}/ocr`, {
      method: "POST",
      body: { pages, engine },
    });
    if (!r.ok) return toast(r.msg || "启动失败", "err");
    toast(
      engine === "vision" ? "精细识别已开始，每页约 40 秒" : "识别已开始，进度看这里",
      "ok"
    );
    pollOcr();
  } catch (e) {
    toast(e.message, "err");
  }
}

async function pollOcr() {
  try {
    const s = await api("/api/ocr/state");
    const batch = !!s.batch;

    // 批量识别换了书：抽屉跟着切过去，否则会一直停在第一本上
    if (
      batch && ocrFollow &&
      $("#ocr-drawer").classList.contains("open") &&
      s.book_id && (!ocrBook || ocrBook.id !== s.book_id)
    ) {
      attachOcrBook(s);
    }
    // 书名是任务线程读到 PDF 之后才回填的，抽屉标题要跟着补上
    if (ocrBook && s.book_id === ocrBook.id && s.book_title && ocrBook.title !== s.book_title) {
      ocrBook.title = s.book_title;
      $("#ocr-title").textContent = s.book_title;
    }

    paintOcrTask(s);

    // 实时预览：正在识别的页往前走了，自动翻到那一页
    if (batch && s.running && ocrBook && s.book_id === ocrBook.id && s.page > 0) {
      const pno = s.page - 1;
      if (ocrAutoPage && pno !== ocrPageNo) {
        selectOcrPage(pno, true);
        loadOcrPages();
      }
    }

    // 批量期间两本书之间 running 会短暂为 false，不能因此停掉轮询
    if (s.running || batch) {
      if (!ocrPoll) ocrPoll = setInterval(pollOcr, 2000);
    } else if (ocrPoll) {
      clearInterval(ocrPoll);
      ocrPoll = null;
      loadOcrPages();
      loadLibrary();
    }
  } catch (_) {}
}

function paintOcrTask(s) {
  const busy = !!s.running || !!s.batch;
  const isCur = !!ocrBook && s.book_id === ocrBook.id;
  $("#ocr-start").disabled = busy;
  $("#ocr-stop").disabled = !busy;
  const bar = $("#ocr-bar");
  if (s.running) {
    const pct = s.total ? Math.round((s.done / s.total) * 100) : 0;
    if (s.batch && ocrBook && !isCur) {
      // 抽屉里看的是别的书：只报批量总进度，别把当前书的页数糊上来
      const idx = Math.min((s.batch_done || 0) + 1, s.batch_total || 0);
      bar.style.width = "0%";
      $("#ocr-status").textContent =
        `批量识别中 · 第 ${idx}/${s.batch_total || 0} 本正在识别，本书排在后面`;
      return;
    }
    bar.style.width = pct + "%";
    const outer = s.batch
      ? `批量 第 ${Math.min((s.batch_done || 0) + 1, s.batch_total || 0)}/${s.batch_total || 0} 本 · `
      : "";
    $("#ocr-status").textContent =
      `${outer}${s.done}/${s.total} 页 · 第 ${s.page} 页 · ${s.chars} 字 · ${s.elapsed}s`;
  } else if (s.batch) {
    // 两本之间的空档，别把进度条清空
    $("#ocr-status").textContent =
      `批量识别中 · 已完成 ${s.batch_done || 0}/${s.batch_total || 0} 本 · 正在切下一本`;
  } else {
    bar.style.width = "0%";
    $("#ocr-status").textContent = "";
  }
}

$("#ocr-close").addEventListener("click", closeOcr);
$("#ocr-start").addEventListener("click", startOcr);

$("#ocr-stop").addEventListener("click", async () => {
  await api("/api/ocr/stop", { method: "POST" });
  toast("已发送停止指令");
});

$("#ocr-save").addEventListener("click", async () => {
  if (!ocrBook || ocrPageNo === null) return toast("先选一页", "err");
  try {
    await api(`/api/library/books/${ocrBook.id}/ocr/${ocrPageNo}`, {
      method: "PUT",
      body: { text: $("#ocr-text").value },
    });
    toast("已保存", "ok");
    loadOcrPages();
  } catch (e) {
    toast(e.message, "err");
  }
});

$("#ocr-redo").addEventListener("click", async () => {
  if (!ocrBook) return toast("先选一页", "err");
  const engine = $("#ocr-engine").value;
  toast(engine === "vision" ? "精细重识别中，约 40 秒…" : "重识别中…");
  try {
    const r = await api(`/api/library/books/${ocrBook.id}/ocr/${ocrPageNo}/redo`, {
      method: "POST",
      body: { engine },
    });
    if (!r.ok) return toast(r.msg || "失败", "err");
    $("#ocr-text").value = r.text || "";
    $("#ocr-page-info").textContent = `${r.chars} 字 · ${engine} · ${r.elapsed}s`;
    toast(`重识别完成：${r.chars} 字`, "ok");
    loadOcrPages();
  } catch (e) {
    toast(e.message, "err");
  }
});

$("#ocr-import").addEventListener("click", async () => {
  if (!ocrBook) return;
  try {
    const r = await api("/api/library/import", {
      method: "POST",
      body: { book_ids: [ocrBook.id], topic: $("#lib-topic").value.trim() },
    });
    if (!r.ok) return toast(r.msg || "启动失败", "err");
    toast("已开始提炼入库，进度看「导入操作」那一栏", "ok");
    closeOcr();
  } catch (e) {
    toast(e.message, "err");
  }
});

/* ---------------- 资源 ---------------- */
async function loadResources() {
  try {
    const r = await api("/api/resources");
    const d = r.disk;
    $("#r-used").textContent = `${d.used_gb} / ${d.budget_gb} GB`;
    $("#r-bar").style.width = Math.min(100, (d.used_gb / d.budget_gb) * 100) + "%";
    $("#r-free").textContent = d.free_gb + " GB";
    // state.cfg 只有打开过「设置」才有；直接用状态接口里的 config 兜底，
    // 否则第一次进资源页永远显示「关闭」（即使设置里是开着的）
    const cfg = state.cfg || state.status?.config || {};
    $("#r-auto").textContent = cfg.auto_download ? "开启" : "关闭";

    const box = $("#res-list");
    if (!r.items.length) {
      box.innerHTML = '<div class="empty">还没有下载任何资源</div>';
      return;
    }
    const ST = {
      done: ["已完成", "var(--green)"],
      downloading: ["下载中", "var(--accent)"],
      checking: ["校验中", "var(--amber)"],
      blocked: ["已跳过", "var(--amber)"],
      failed: ["失败", "var(--red)"],
      pending: ["排队", "var(--text-dim)"],
    };
    box.innerHTML = r.items
      .map((it) => {
        const [txt, color] = ST[it.status] || [it.status, "var(--text-dim)"];
        return `<div class="row">
          <div class="rowflex" style="justify-content:space-between">
            <span class="mono" style="font-size:13px">${esc(it.repo)}</span>
            <span style="color:${color};font-size:12px">${txt}</span>
          </div>
          <div class="meta">
            <span class="tagmini">${esc(it.rtype)}</span>
            <span>${it.size_gb || 0} GB</span>
            ${it.direction ? `<span class="tagmini">${esc(it.direction)}</span>` : ""}
            <span style="margin-left:auto">${fmtTime(it.created_at)}</span>
          </div>
          ${it.reason ? `<div class="detail">${esc(it.reason).slice(0, 200)}</div>` : ""}
        </div>`;
      })
      .join("");
  } catch (e) {
    toast("资源加载失败：" + e.message, "err");
  }
}

$("#btn-res-add").addEventListener("click", async () => {
  const repo = $("#res-repo").value.trim();
  if (!repo) return toast("填写仓库名", "err");
  toast("已提交，正在校验体积与预算…");
  try {
    const r = await api("/api/resources/download", {
      method: "POST",
      body: { repo, kind: $("#res-kind").value },
    });
    toast(r.ok ? "下载完成" : r.msg || "已跳过", r.ok ? "ok" : "err");
    loadResources();
  } catch (e) {
    toast("提交失败：" + e.message, "err");
  }
});

/* ---------------- 微调 ---------------- */
async function loadFinetune() {
  try {
    const env = await api("/api/finetune/env");
    const rows = Object.entries(env.modules)
      .map(
        ([k, v]) =>
          `<div class="kv"><span class="k">${k}</span><span class="v ${
            v ? "ok-mark" : "bad-mark"
          }">${v || "未安装"}</span></div>`
      )
      .join("");
    $("#ft-env").innerHTML =
      rows +
      `<div class="kv"><span class="k">CUDA</span><span class="v ${
        env.cuda ? "ok-mark" : "bad-mark"
      }">${env.cuda ? "可用" : "不可用"}</span></div>
       <div class="kv"><span class="k">显卡</span><span class="v">${esc(env.gpu || "—")}</span></div>`;

    const outs = await api("/api/finetune/outputs");
    const box = $("#ft-outputs");
    if (!outs.length) {
      box.innerHTML = '<div class="empty">还没有产出</div>';
    } else {
      box.innerHTML = outs
        .map(
          (o) => `<div class="row">
            <div class="rowflex" style="justify-content:space-between">
              <span class="mono" style="font-size:12.5px">${esc(o.name)}</span>
              <span class="tagmini">${o.kind}</span>
            </div>
            <div class="meta"><span>${o.size_gb} GB</span>
              <span style="margin-left:auto">${fmtTime(o.mtime)}</span></div>
          </div>`
        )
        .join("");
    }
  } catch (e) {
    toast("微调信息加载失败：" + e.message, "err");
  }
}

$("#btn-ft-install").addEventListener("click", async () => {
  toast("开始安装，约 3GB，请看进化流日志");
  await api("/api/finetune/install", { method: "POST" });
});

let ftPollTimer = null;

// 生成训练集要改写几百条问句，十几分钟起步，必须轮询看进度
function pollFinetune() {
  if (ftPollTimer) return;
  ftPollTimer = setInterval(async () => {
    try {
      const st = await api("/api/finetune/state");
      const pct = Math.round((st.progress || 0) * 100);
      const bar = $("#ft-bar");
      if (bar) bar.style.width = pct + "%";
      // 进度条只在跑的时候出现：平时常驻一条 0% 的空槽，看起来像分割线
      const pw = $("#ft-progress");
      if (pw) pw.hidden = !st.running;
      if (st.running && st.stage) $("#ft-ds").textContent = st.stage;
      if (!st.running) {
        if (pw) pw.hidden = true;
        clearInterval(ftPollTimer);
        ftPollTimer = null;
        $("#btn-ft-dataset").disabled = false;
        $("#btn-ft-train").disabled = false;
        const res = st.result || {};
        if (res.ok && res.count !== undefined) {
          // 数据集生成完成
          state.datasetPath = res.path;
          $("#ft-ds").textContent = `${res.count} 条`;
          $("#btn-ft-train").disabled = false;
          toast(`训练集已生成：${res.count} 条`, "ok");
        } else if (res.ok) {
          // 微调完成
          $("#ft-train").textContent = "已完成";
          $("#ft-merge").textContent = res.merged ? "已合并" : "仅 adapter";
          toast("微调完成", "ok");
        } else if (res.msg) {
          toast(res.msg, "err");
        }
      }
    } catch (e) {
      // 单次轮询失败不打断，等下一轮
    }
  }, 2000);
}

$("#btn-ft-dataset").addEventListener("click", async () => {
  try {
    const r = await api("/api/finetune/dataset", { method: "POST", body: {} });
    if (!r.ok) return toast(r.msg || "启动失败", "err");
    $("#btn-ft-dataset").disabled = true;
    $("#ft-ds").textContent = "准备中…";
    const pw = $("#ft-progress");
    if (pw) pw.hidden = false;
    toast("已开始生成训练集，进度见上方进度条");
    pollFinetune();
  } catch (e) {
    toast("启动失败：" + e.message, "err");
  }
});

$("#btn-ft-train").addEventListener("click", async () => {
  if (!state.datasetPath) return toast("先生成训练集", "err");
  try {
    const r = await api("/api/finetune/train", {
      method: "POST",
      body: { dataset_path: state.datasetPath },
    });
    if (!r.ok) return toast(r.msg || "启动失败", "err");
    toast(r.msg || "已启动", "ok");
    $("#btn-ft-train").disabled = true;
    $("#ft-train").textContent = "训练中…";
    pollFinetune();
  } catch (e) {
    toast("启动失败：" + e.message, "err");
  }
});

/* ---------------- 对话 ---------------- */
async function askChat() {
  const q = $("#chat-q").value.trim();
  if (!q || state.chatBusy) return;
  state.chatBusy = true;
  $("#chat-q").value = "";
  const log = $("#chat-log");

  // 第一句发出后示例提问就收起，避免占着对话区
  const hints = $("#chat-hints");
  if (hints) hints.hidden = true;

  const u = document.createElement("div");
  u.className = "msg user";
  u.textContent = q;
  log.appendChild(u);

  const b = document.createElement("div");
  b.className = "msg bot";
  b.textContent = "思考中…";
  log.appendChild(b);
  log.scrollTop = log.scrollHeight;

  try {
    const r = await api("/api/chat", { method: "POST", body: { question: q } });
    b.textContent = r.answer;
    if (r.sources?.length) {
      const s = document.createElement("div");
      s.className = "srcs";
      s.innerHTML =
        `<div class="dim">引用 ${r.hits} 条知识</div>` +
        r.sources
          .filter((x) => x.url)
          .slice(0, 5)
          .map(
            (x) =>
              `<a href="${esc(x.url)}" target="_blank" rel="noreferrer">${esc(
                x.title || x.url
              ).slice(0, 80)}</a>`
          )
          .join("");
      b.appendChild(s);
    }
  } catch (e) {
    b.textContent = "出错了：" + e.message;
  }
  log.scrollTop = log.scrollHeight;
  state.chatBusy = false;
}

$("#btn-chat").addEventListener("click", askChat);
document.addEventListener("click", (e) => {
  const ask = e.target.closest("[data-ask]");
  if (!ask) return;
  const q = $("#chat-q");
  if (!q) return;
  q.value = ask.dataset.ask || "";
  q.focus();
});

$("#chat-q").addEventListener("keydown", (e) => {
  if (e.key === "Enter") askChat();
});

/* ---------------- 设置 ---------------- */
async function fillSettings() {
  try {
    const [cfg, models] = await Promise.all([api("/api/config"), api("/api/models")]);
    state.cfg = cfg;
    const hint = $("#cfg-model-hint");
    $("#cfg-model").innerHTML = models.length
      ? models
          .map(
            (m) =>
              `<option value="${esc(m.name)}" ${m.name === cfg.model ? "selected" : ""}>${esc(
                m.name
              )} · ${m.size_gb}GB ${m.caps?.includes("tools") ? "· 工具调用" : ""}</option>`
          )
          .join("")
      : `<option value="${esc(cfg.model)}">${esc(cfg.model)} · 未检测到</option>`;
    // 一个模型都没探到，通常不是「没装模型」而是 Ollama 没启动——直接说清怎么修
    if (hint) {
      hint.hidden = models.length > 0;
      hint.innerHTML = models.length
        ? ""
        : `没读到模型清单。多数情况是 <b>Ollama 没启动</b>：开一个终端跑
           <code>ollama serve</code>（或直接启动 Ollama 应用），然后刷新本页。
           若 Ollama 在别的地址，改下面的「服务地址」再试。`;
    }
    // 探测是异步的：首帧可能还没回来（服务端改成后台探测，不再阻塞设置页）。
    // 2.5 秒后补拉一次，模型清单到了就自动填上，用户不用手动刷新。
    if (!models.length) {
      setTimeout(() => {
        if (state.view !== "settings") return;
        api("/api/models")
          .then((later) => {
            if (later && later.length) fillSettings();
          })
          .catch(() => {});
      }, 2500);
    }
    $("#cfg-embed").value = cfg.embed_model;
    $("#cfg-qpr").value = cfg.queries_per_round;
    $("#cfg-fpr").value = cfg.fetch_per_round;
    $("#cfg-rel").value = cfg.min_relevance;
    $("#cfg-budget").value = cfg.disk_budget_gb;
    $("#cfg-maxone").value = cfg.max_model_gb;
    $("#cfg-auto").value = cfg.auto_download ? "1" : "0";
    $("#cfg-hf").value = cfg.hf_endpoint || "";
    $("#cfg-proxy").value = cfg.proxy || "";
    $("#cfg-search-key").value = cfg.search_api_key || "";
    $("#cfg-ft-base").value = cfg.finetune_base || "";
    // 多模态（老配置文件没有这些键时用默认值兜底，避免显示成空）
    $("#cfg-media").value = cfg.media_enabled === false ? "0" : "1";
    $("#cfg-media-per").value = cfg.media_per_round ?? 2;
    $("#cfg-media-max").value = cfg.media_max_mb ?? 80;
    $("#cfg-asr-model").value = cfg.asr_model || "small";
    $("#cfg-vision").value = cfg.vision_model || "";
  } catch (e) {
    toast("设置加载失败：" + e.message, "err");
  }
}

$("#btn-save-cfg").addEventListener("click", async () => {
  try {
    await api("/api/config", {
      method: "POST",
      body: {
        model: $("#cfg-model").value,
        embed_model: $("#cfg-embed").value.trim(),
        queries_per_round: parseInt($("#cfg-qpr").value, 10),
        fetch_per_round: parseInt($("#cfg-fpr").value, 10),
        min_relevance: parseFloat($("#cfg-rel").value),
        disk_budget_gb: parseInt($("#cfg-budget").value, 10),
        max_model_gb: parseInt($("#cfg-maxone").value, 10),
        auto_download: $("#cfg-auto").value === "1",
        hf_endpoint: $("#cfg-hf").value.trim(),
        proxy: $("#cfg-proxy").value.trim(),
        search_api_key: $("#cfg-search-key").value.trim(),
        finetune_base: $("#cfg-ft-base").value.trim(),
      },
    });
    toast("设置已保存", "ok");
  } catch (e) {
    toast("保存失败：" + e.message, "err");
  }
});

/* 多模态设置单独一个保存按钮：这块开关直接影响一轮学习的耗时，
   和上面的模型/预算设置混在一起容易被误改。 */
$("#btn-save-cfg-media")?.addEventListener("click", async () => {
  try {
    await api("/api/config", {
      method: "POST",
      body: {
        media_enabled: $("#cfg-media").value === "1",
        media_per_round: parseInt($("#cfg-media-per").value, 10) || 0,
        media_max_mb: parseInt($("#cfg-media-max").value, 10) || 80,
        asr_model: $("#cfg-asr-model").value,
        vision_model: ($("#cfg-vision").value || "").trim() || "qwen2.5vl:7b",
      },
    });
    toast("多模态设置已保存", "ok");
    if (state.view === "media") loadMediaCaps();
  } catch (e) {
    toast("保存失败：" + e.message, "err");
  }
});

/* ---------------- 分析视图 ---------------- */
/* 全部图表用纯 SVG 现画（不引第三方库，和前端「无框架」的基调一致）。
   主题色直接用 CSS 变量，换肤也跟着变。

   尺寸策略（这一版的关键改动）：
   旧实现给每张图一个固定的 viewBox（680×240 之类），再由 CSS 的 width:100%
   拉满卡片。卡片 1010px 宽时等于整体放大 1.49 倍——10px 的轴标签变成 15px、
   1px 网格线变成 1.5px、热力图的 13px 格子变成 39px 色块，几张图比例各不相同，
   摆在一起就显得「格格不入」。
   现在改成：ResizeObserver 量出容器的真实像素宽度，按这个宽度现画，
   svg 的 width/height 就是 1:1 的像素尺寸，字号与线宽永远是设计值；
   卡片变宽只会多显示内容，不会把字撑大。 */
const AN_PALETTE = [
  "var(--accent)", "var(--green)", "var(--amber)", "var(--purple)",
  "var(--red)", "#5ad0ff", "#ff9bd0", "#7be0a0",
];

const _mc = (() => {
  try {
    const c = document.createElement("canvas");
    return c && typeof c.getContext === "function" ? c.getContext("2d") : null;
  } catch (_) {
    return null;
  }
})();

/* 文本宽度：能用 canvas 就精确量；量不了（无 canvas 的极简 DOM / 测试桩）
   就按字符估算——中文一个字约 1em，ASCII 约 0.55em。布局只需要个近似值。 */
function textW(str, size = 10.5, mono = false) {
  const s = String(str);
  if (_mc && typeof _mc.measureText === "function") {
    _mc.font = `${size}px ${mono ? "Consolas, ui-monospace, monospace" : '-apple-system, "Microsoft YaHei", sans-serif'}`;
    return _mc.measureText(s).width;
  }
  let w = 0;
  for (const ch of s) w += /[\u2e80-\u9fff\uff00-\uffef]/.test(ch) ? size : size * 0.55;
  return w;
}

function fmtNum(v) {
  return Number(v || 0).toLocaleString("en-US");
}

/* 轴刻度用「好看的数字」：1/2/2.5/5 × 10^n，避免出现 1776、5328 这种刻度 */
function niceScale(v, count = 4) {
  v = Math.max(0, Number(v) || 0);
  if (v <= 0) return { max: 1, step: 1 };
  const raw = v / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const n = raw / mag;
  const step = (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * mag;
  return { max: Math.ceil(v / step) * step, step };
}

function fmtAxis(v) {
  if (v >= 10000) {
    const x = v / 10000;
    return (Number.isInteger(x) ? x : x.toFixed(1)) + "万";
  }
  return String(Math.round(v));
}

function emptyChart(msg, h = 130) {
  return `<div class="empty" style="height:${h}px;display:flex;align-items:center;justify-content:center;padding:0">${esc(msg)}</div>`;
}

/* 图表容器：SVG 直接写进 .chart 本身（重绘 = 一次 innerHTML 替换，
   外部据此就能判断「这一轮有没有真的重建」）。悬浮气泡按需挂载。 */
function ensureChart(el) {
  return el;
}

/* 悬浮气泡：第一次悬停时挂上去；下一次重绘会把它连同旧 SVG 一起换掉，
   所以这里每次都确认它还在文档里。 */
function chartTip(el) {
  if (!el.__tip || el.__tip.parentNode !== el) {
    const t = document.createElement("div");
    t.className = "chart-tip";
    t.hidden = true;
    el.appendChild(t);
    el.__tip = t;
  }
  return el.__tip;
}

/* height 可以是数字，也可以是 () => 数字（热力图高度随格子大小走） */
function chartFrame(el, height, draw) {
  if (!el) return;
  el.__draw = draw;
  el.__h = height;
  if (!el.__ro && typeof ResizeObserver === "function") {
    el.__ro = new ResizeObserver(() => {
      if (Math.round(el.clientWidth) === el.__w) return;
      paintChart(el);
    });
    el.__ro.observe(el);
  }
  paintChart(el);
}

function paintChart(el) {
  if (!el || !el.__draw) return;
  /* 视图被切到别处时（display:none）容器宽度会变成 0，此时不要重绘：
     否则图会被画成 220px 的小图，切回来还要再闪一下。
     offsetParent === null 正是 display:none 的判据。 */
  if (el.offsetParent === null) return;
  const cw = Math.round(el.clientWidth) || 0;
  const w = cw > 0 ? cw : 680; // 量不到宽度时退回设计宽度（无布局的极简 DOM）
  el.__w = w;
  const h = typeof el.__h === "function" ? el.__h() : el.__h;
  let svg = "";
  try {
    svg = el.__draw(w, h);
  } catch (e) {
    svg = emptyChart("图表渲染失败：" + (e && e.message ? e.message : e), 120);
  }
  if (el.__tip) el.__tip.hidden = true; // 重绘会替换掉气泡，先收起
  setHTML(el, svg);
}

/* ---------------- 知识增长曲线 ---------------- */
function growthGeom(w, h, growth) {
  const padR = 14, padT = 18, padB = 26;
  const n = growth.length;
  const cmax = Math.max(...growth.map((d) => d.cumulative), 1);
  const sc = niceScale(cmax, 4);
  const ticks = [];
  for (let v = 0; v <= sc.max + sc.step * 0.001; v += sc.step) ticks.push(v);
  const tickLabels = ticks.map(fmtAxis);
  const padL = Math.ceil(Math.max(...tickLabels.map((t) => textW(t, 10.5))) + 12);
  const pw = Math.max(40, w - padL - padR);
  const ph = Math.max(40, h - padT - padB);
  const amax = Math.max(...growth.map((d) => d.added), 1);
  const barH = ph * 0.4;
  const X = (i) => padL + (n === 1 ? pw / 2 : (i / (n - 1)) * pw);
  const Y = (v) => padT + ph * (1 - Math.min(1, Math.max(0, v / sc.max)));
  const step = n > 1 ? pw / (n - 1) : pw;
  return { w, h, padL, padR, padT, padB, pw, ph, n, ticks, tickLabels, sc, amax, barH, X, Y, step, growth };
}

function drawGrowth(w, h, growth) {
  if (!growth || !growth.length) return emptyChart("还没有知识增长数据", h);
  const g = growthGeom(w, h, growth);
  const { padL, padT, pw, ph, n, ticks, tickLabels, X, Y, step, amax, barH } = g;
  const base = padT + ph;

  let s = `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="知识增长曲线">`;

  // 网格 + 左侧刻度（0 线不加，避免和柱子底部叠成一条粗线）
  ticks.forEach((v, i) => {
    const y = Y(v);
    if (v > 0) s += `<line class="grid-line dash" x1="${padL}" y1="${y.toFixed(1)}" x2="${(padL + pw).toFixed(1)}" y2="${y.toFixed(1)}"/>`;
    s += `<text class="axis-num" x="${padL - 8}" y="${(y + 3.5).toFixed(1)}" text-anchor="end">${tickLabels[i]}</text>`;
  });

  // 每日新增（柱）——独立标尺，压在折线下方，不抢累计曲线的视线
  const bw = Math.max(2, Math.min(13, step * 0.55));
  let bars = "";
  growth.forEach((d, i) => {
    if (!d.added) return;
    const bh = Math.max(1, (d.added / amax) * barH);
    const x = X(i) - bw / 2;
    bars += `<rect x="${x.toFixed(1)}" y="${(base - bh).toFixed(1)}" width="${bw.toFixed(1)}" height="${bh.toFixed(1)}" rx="${Math.min(2, bw / 2).toFixed(1)}" fill="var(--accent)" opacity="0.62"/>`;
  });

  // 累计（线 + 渐变面积）
  let line = "", area = "";
  if (n > 1) {
    const pts = growth.map((d, i) => [X(i), Y(d.cumulative)]);
    line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
    area = `M${pts[0][0].toFixed(1)} ${base} ` +
      pts.map((p) => "L" + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ") +
      ` L${pts[pts.length - 1][0].toFixed(1)} ${base} Z`;
  }
  const lastX = X(n - 1), lastY = Y(growth[n - 1].cumulative);

  // 横轴日期：按可用宽度决定标注密度，首尾必留
  const maxLab = Math.max(2, Math.floor(pw / 76));
  const stepIdx = Math.max(1, Math.ceil((n - 1) / (maxLab - 1)));
  let xl = "";
  for (let i = n - 1; i >= 0; i -= stepIdx) {
    xl += `<text class="axis-num" x="${X(i).toFixed(1)}" y="${h - 7}" text-anchor="middle">${esc(growth[i].day.slice(5))}</text>`;
  }

  // 悬浮层：竖线 + 高亮点（默认隐藏，指针移动时由 JS 定位）
  const hover = `<g class="hv" style="opacity:0">
      <line class="hv-line" x1="0" y1="${padT}" x2="0" y2="${base}" stroke="var(--text-faint)" stroke-width="1" stroke-dasharray="3 3"/>
      <circle class="hv-dot" cx="0" cy="0" r="4" fill="var(--green)" stroke="#0a0f16" stroke-width="2"/>
    </g>`;

  s += `<defs><linearGradient id="grad-growth" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="var(--green)" stop-opacity="0.18"/>
      <stop offset="100%" stop-color="var(--green)" stop-opacity="0.01"/>
    </linearGradient></defs>`;
  if (area) s += `<path d="${area}" fill="url(#grad-growth)"/>`;
  s += bars; // 柱子压在面积之上，否则会被渐变吃掉一半颜色
  if (line) s += `<path d="${line}" fill="none" stroke="var(--green)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  s += `<circle cx="${lastX.toFixed(1)}" cy="${lastY.toFixed(1)}" r="7" fill="var(--green)" opacity="0.16"/>`;
  s += `<circle cx="${lastX.toFixed(1)}" cy="${lastY.toFixed(1)}" r="3.2" fill="var(--green)" stroke="#0a0f16" stroke-width="1.5"/>`;
  s += hover + xl;
  return s + `</svg>`;
}

function mountGrowth(el, growth) {
  chartFrame(el, 272, (w, h) => drawGrowth(w, h, growth));
  el.__growth = growth || [];
  if (el.__hoverBound) return;
  el.__hoverBound = true;

  const hide = () => {
    const svg = el.querySelector("svg");
    const hv = svg && svg.querySelector("g.hv");
    if (hv) hv.style.opacity = "0";
    if (el.__tip) el.__tip.hidden = true;
  };

  el.addEventListener("pointermove", (e) => {
    const data = el.__growth;
    const svg = el.querySelector("svg");
    if (!data || !data.length || !svg || !el.__w) return hide();
    const g = growthGeom(el.__w, typeof el.__h === "number" ? el.__h : 272, data);
    const r = svg.getBoundingClientRect();
    const x = e.clientX - r.left;
    if (x < g.padL - 6 || x > g.padL + g.pw + 6) return hide();
    const i = Math.max(0, Math.min(g.n - 1, Math.round((x - g.padL) / (g.n === 1 ? 1 : g.step))));
    const d = data[i];
    const px = g.X(i), py = g.Y(d.cumulative);
    const hv = svg.querySelector("g.hv");
    if (hv) {
      hv.style.opacity = "1";
      const ln = hv.querySelector(".hv-line");
      ln.setAttribute("x1", px.toFixed(1));
      ln.setAttribute("x2", px.toFixed(1));
      const dot = hv.querySelector(".hv-dot");
      dot.setAttribute("cx", px.toFixed(1));
      dot.setAttribute("cy", py.toFixed(1));
    }
    {
      const tip = chartTip(el);
      tip.hidden = false;
      tip.innerHTML =
        `<div class="d">${esc(d.day)}</div>` +
        `<div class="r"><span>每日新增</span><b>${fmtNum(d.added)}</b></div>` +
        `<div class="r"><span>累计</span><b>${fmtNum(d.cumulative)}</b></div>`;
      // 气泡贴着点往上，左右贴边时自动收进来，不出卡片
      const tw = tip.offsetWidth || 110;
      const left = Math.max(tw / 2 + 2, Math.min((el.__w || 680) - tw / 2 - 2, px));
      tip.style.left = left + "px";
      tip.style.top = Math.max(4, py - 12) + "px";
    }
  });
  el.addEventListener("pointerleave", hide);
}

/* ---------------- 学习活跃度热力图 ---------------- */
function heatStats(growth) {
  const added = {};
  growth.forEach((g) => (added[g.day] = g.added || 0));
  const days = Object.keys(added).sort();
  if (!days.length) return null;
  const total = days.reduce((a, k) => a + added[k], 0);
  const active = days.filter((k) => added[k] > 0).length;
  const max = Math.max(...days.map((k) => added[k]));
  // 连续天数：从最后一天往回数，遇到 0 就停
  let streak = 0;
  for (let i = days.length - 1; i >= 0; i--) {
    if (added[days[i]] > 0) streak++;
    else break;
  }
  return {
    active, max, total, streak,
    avg: days.length ? total / days.length : 0,
  };
}

function drawHeatmap(w, growth) {
  const added = {};
  (growth || []).forEach((g) => (added[g.day] = g.added || 0));
  const days = (growth || []).map((g) => g.day).sort();
  if (!days.length) return { svg: emptyChart("还没有学习记录", 150), h: 150 };
  const first = new Date(days[0] + "T00:00:00Z");
  const last = new Date(days[days.length - 1] + "T00:00:00Z");
  const firstSun = new Date(first);
  firstSun.setUTCDate(first.getUTCDate() - first.getUTCDay()); // 对齐到周日
  const spanDays = Math.round((last - firstSun) / 86400000) + 1;
  const spanWeeks = Math.ceil(spanDays / 7);
  /* 至少铺 12 周，而且补出来的空白周要补在「左边」（更早的时间），
     让有数据的日子始终贴在最右边——和日历直觉一致。
     旧实现补在右边，2 天数据会变成「左边一格有内容、右边十一格全空」。 */
  const weeks = Math.max(spanWeeks, 12);
  const start = new Date(firstSun.getTime() - (weeks - spanWeeks) * 7 * 86400000);
  const totalDays = Math.round((last - start) / 86400000) + 1;
  const left = 26, top = 20, gap = 3;
  const cell = Math.max(9, Math.min(28, Math.floor((w - left - 6) / weeks) - gap));
  const W = left + weeks * (cell + gap) - gap;
  const gridH = top + 7 * (cell + gap) - gap;
  const H = gridH + 27; // 底部留一行给色阶图例，否则图例落在 viewBox 外被裁掉
  const maxAdd = Math.max(...(growth || []).map((g) => g.added), 1);
  /* 五档色阶。分档按「分位数」而不是占最大值的比例：一次集中导入的尖峰会把
     比例压扁，让其余日子全落进同一档、整张图变成一块同色。
     取分位数后，无论数据多偏，四档都能均匀铺开。 */
  const HEAT = [
    ["rgba(255,255,255,.045)", 1],
    ["rgba(33,212,160,.16)", 1],
    ["rgba(33,212,160,.34)", 1],
    ["rgba(33,212,160,.56)", 1],
    ["rgba(33,212,160,.86)", 1],
  ];
  const pos = (growth || []).map((g) => g.added || 0).filter((n) => n > 0).sort((a, b) => a - b);
  const qAt = (p) => (pos.length ? pos[Math.min(pos.length - 1, Math.floor(p * (pos.length - 1)))] : 1);
  const t1 = qAt(0.25), t2 = qAt(0.55), t3 = qAt(0.82);
  const tierOf = (n) => (n <= 0 ? 0 : n <= t1 ? 1 : n <= t2 ? 2 : n <= t3 ? 3 : 4);
  const styleOf = (n) => HEAT[tierOf(n)];
  const WD = ["日", "一", "二", "三", "四", "五", "六"];
  let s = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="学习活跃度热力图">`;
  // 周标签：一/三/五 三档，够看又不挤
  [1, 3, 5].forEach((d) => {
    s += `<text class="axis-text" x="0" y="${(top + d * (cell + gap) + cell / 2 + 3.5).toFixed(1)}">${WD[d]}</text>`;
  });
  // 月份：某一列跨月时在顶部标一次
  let lastMonth = -1;
  for (let wk = 0; wk < weeks; wk++) {
    const dt = new Date(start.getTime() + wk * 7 * 86400000);
    const m = dt.getUTCMonth();
    if (m !== lastMonth) {
      lastMonth = m;
      s += `<text class="axis-text" x="${(left + wk * (cell + gap)).toFixed(1)}" y="12">${m + 1}月</text>`;
    }
  }
  // 从网格第一列开始铺（空白日也用极淡的底色占位），日历格子才是连续的
  for (let i = 0; i < totalDays; i++) {
    const dt = new Date(start.getTime() + i * 86400000);
    if (dt > last) break;
    const wd = dt.getUTCDay();
    const wk = Math.floor(i / 7);
    const key = dt.toISOString().slice(0, 10);
    const n = added[key] || 0;
    const [col, op] = styleOf(n);
    const x = left + wk * (cell + gap);
    const y = top + wd * (cell + gap);
    s += `<rect x="${x}" y="${y}" width="${cell}" height="${cell}" rx="${Math.max(2, cell / 5).toFixed(1)}" fill="${col}" opacity="${op}"><title>${key}：新增 ${n} 条</title></rect>`;
  }
  // 右下角色阶图例：固定 11px 的小方块，不跟着格子放大
  const lg = 11, lgGap = 3, ly = gridH + 12;
  const lx = W - (lg + lgGap) * 4 + lgGap + 2;
  s += `<text class="axis-text" x="${lx - 22}" y="${ly + lg - 1.5}">少</text>`;
  HEAT.slice(1).forEach(([col, op], i) => {
    s += `<rect x="${(lx + i * (lg + lgGap)).toFixed(1)}" y="${ly}" width="${lg}" height="${lg}" rx="2.5" fill="${col}" opacity="${op}"/>`;
  });
  s += `<text class="axis-text" x="${lx + 4 * (lg + lgGap) + 3}" y="${ly + lg - 1.5}">多</text>`;
  return { svg: s + `</svg>`, h: H };
}

function mountHeatmap(el, side, growth) {
  chartFrame(el, () => 170, (w) => drawHeatmap(w, growth).svg);
  // 高度随格子走：格子由宽度算出来，画完再把容器高度同步给 svg（svg 自带 height，无需额外处理）
  const st = heatStats(growth || []);
  if (!side) return;
  if (!st) {
    setHTML(side, "");
    return;
  }
  const box = (k, v) => `<div class="an-heat-stat"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  setHTML(
    side,
    box("活跃天数", st.active + " 天") +
      box("最长连续", st.streak + " 天") +
      box("单日最高", fmtNum(st.max)) +
      box("日均新增", st.avg.toFixed(1))
  );
}

/* ---------------- 能力分布（HTML 条） ---------------- */
function htmlTopicBars(topics) {
  if (!topics || !topics.length) return '<div class="empty">还没有任何学习记录</div>';
  const max = Math.max(...topics.map((t) => t.count));
  return topics.slice(0, 12).map((t) =>
    // kb-drill：点一下跳到知识库并按该方向筛选（见全局委托处理器）
    `<div class="kb-drill" data-topic="${esc(t.topic)}" title="点击在知识库中筛选该方向">
      <div class="rowflex" style="justify-content:space-between;font-size:12.5px;gap:10px">
        <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(t.topic)}</span>
        <span class="mono dim" style="flex:0 0 auto;font-size:11px">${t.count} · ${(+t.avg_conf).toFixed(2)}</span>
      </div>
      <div class="bar" style="margin-top:6px"><i style="width:${(t.count / max * 100).toFixed(1)}%"></i></div>
    </div>`
  ).join("");
}

/* ---------------- 可信度直方图 ---------------- */
function drawHist(w, h, confidence) {
  if (!confidence || !confidence.length) return emptyChart("暂无数据", h);
  const padL = 34, padR = 12, padT = 20, padB = 26;
  const pw = Math.max(40, w - padL - padR), ph = Math.max(30, h - padT - padB);
  const n = confidence.length;
  const maxn = Math.max(...confidence.map((b) => b.n), 1);
  const sc = niceScale(maxn, 3);
  const base = padT + ph;
  const bw = pw / n;
  const Y = (v) => base - (v / sc.max) * ph;
  let s = `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="可信度分布直方图">`;
  for (let i = 0; i <= 3; i++) {
    const v = (sc.max / 3) * i;
    const y = Y(v);
    if (i > 0) s += `<line class="grid-line dash" x1="${padL}" y1="${y.toFixed(1)}" x2="${(padL + pw).toFixed(1)}" y2="${y.toFixed(1)}"/>`;
    s += `<text class="axis-num" x="${padL - 7}" y="${(y + 3.5).toFixed(1)}" text-anchor="end">${fmtAxis(v)}</text>`;
  }
  s += `<line x1="${padL}" y1="${base}" x2="${(padL + pw).toFixed(1)}" y2="${base}" stroke="var(--border)" stroke-width="1"/>`;
  const gap = Math.min(6, bw * 0.22);
  confidence.forEach((b, i) => {
    const bh = (b.n / sc.max) * ph;
    const x = padL + i * bw + gap / 2;
    const t = n > 1 ? i / (n - 1) : 0;
    const col = t < 0.25 ? "var(--red)" : t < 0.5 ? "var(--amber)" : t < 0.75 ? "var(--green)" : "var(--accent)";
    s += `<rect x="${x.toFixed(1)}" y="${(base - bh).toFixed(1)}" width="${(bw - gap).toFixed(1)}" height="${bh.toFixed(1)}" rx="2.5" fill="${col}" opacity="0.85"><title>${(+b.lo).toFixed(1)}–${(+b.hi).toFixed(1)}：${b.n} 条</title></rect>`;
    // 柱子太矮时数值会飘在半空，反而干扰读数——只在柱子够高时标在顶端
    if (bw > 34 && b.n > 0 && bh >= 24) {
      s += `<text class="axis-num" x="${(padL + i * bw + bw / 2).toFixed(1)}" y="${(base - bh - 6).toFixed(1)}" text-anchor="middle" style="fill:var(--text-dim)">${fmtAxis(b.n)}</text>`;
    }
  });
  // 平均可信度参考线（数值放到卡片标题里，图上只留一条虚线，不压住柱子）
  let sum = 0, cnt = 0;
  confidence.forEach((b) => { sum += b.n * ((b.lo + b.hi) / 2); cnt += b.n; });
  if (cnt > 0) {
    const avg = sum / cnt;
    const x = padL + Math.min(n - 0.001, Math.max(0, avg * n)) * bw;
    s += `<line x1="${x.toFixed(1)}" y1="${(padT - 8).toFixed(1)}" x2="${x.toFixed(1)}" y2="${base.toFixed(1)}" stroke="var(--text-faint)" stroke-width="1" stroke-dasharray="3 3" opacity="0.8"/>`;
    s += `<circle cx="${x.toFixed(1)}" cy="${(padT - 8).toFixed(1)}" r="2.5" fill="var(--text-faint)"/>`;
  }
  confidence.forEach((b, i) => {
    if (n > 8 && i % 2) return; // 桶多时隔一个标一次，避免文字打架
    s += `<text class="axis-num" x="${(padL + i * bw + bw / 2).toFixed(1)}" y="${h - 8}" text-anchor="middle">${(+b.lo).toFixed(1)}</text>`;
  });
  // 右端补一个 1.0，区间读起来才是闭合的 [0,1]
  s += `<text class="axis-num" x="${(padL + pw).toFixed(1)}" y="${h - 8}" text-anchor="end">1.0</text>`;
  return s + `</svg>`;
}

function arcPath(cx, cy, rO, rI, a0, a1) {
  const p = (a, r) => [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  const [x0, y0] = p(a0, rO), [x1, y1] = p(a1, rO), [x2, y2] = p(a1, rI), [x3, y3] = p(a0, rI);
  const large = a1 - a0 > Math.PI ? 1 : 0;
  return `M${x0.toFixed(1)} ${y0.toFixed(1)} A${rO} ${rO} 0 ${large} 1 ${x1.toFixed(1)} ${y1.toFixed(1)} ` +
    `L${x2.toFixed(1)} ${y2.toFixed(1)} A${rI} ${rI} 0 ${large} 0 ${x3.toFixed(1)} ${y3.toFixed(1)} Z`;
}

/* ---------------- 来源构成（环形 + 图例） ---------------- */
function drawDonut(w, h, sources) {
  if (!sources || !sources.length) return emptyChart("暂无来源数据", h);
  const total = sources.reduce((a, b) => a + b.count, 0) || 1;
  const size = Math.max(96, Math.min(h - 28, w * 0.34, 212));
  const cx = 14 + size / 2, cy = h / 2;
  const rO = size / 2, rI = rO * 0.63;
  let s = `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="知识来源构成">`;
  let a0 = -Math.PI / 2;
  sources.forEach((src, i) => {
    const frac = src.count / total;
    if (frac <= 0.0005) return;
    const a1 = a0 + frac * Math.PI * 2 * 0.9995;
    const col = AN_PALETTE[i % AN_PALETTE.length];
    s += `<path d="${arcPath(cx, cy, rO, rI, a0, a1)}" fill="${col}" opacity="0.88"><title>${esc(src.kind)}：${src.count} 条（${((frac) * 100).toFixed(1)}%）</title></path>`;
    a0 = a1;
  });
  s += `<text x="${cx}" y="${cy - 1}" text-anchor="middle" style="fill:var(--text);font-family:var(--mono);font-size:${Math.round(size * 0.17)}px;font-weight:600">${fmtNum(total)}</text>`;
  s += `<text x="${cx}" y="${cy + Math.round(size * 0.14)}" text-anchor="middle" class="axis-text">总条数</text>`;

  // 图例：颜色块 + 名称 + 条数/占比 + 细比例条
  const lx = 14 + size + 26;
  const lw = Math.max(80, w - lx - 10);
  const rows = sources.length;
  // 行距按可用高度摊开（上限 46），图例不会挤在卡片上半截
  const rowH = Math.min(42, Math.max(24, (h - 12) / Math.max(1, rows)));
  const startY = Math.max(6, (h - rows * rowH) / 2);
  sources.forEach((src, i) => {
    const col = AN_PALETTE[i % AN_PALETTE.length];
    const pct = (src.count / total) * 100;
    const y = startY + i * rowH;
    const name = src.kind.length > 14 ? src.kind.slice(0, 14) + "…" : src.kind;
    s += `<rect x="${lx}" y="${y + 1}" width="8" height="8" rx="2" fill="${col}"/>`;
    s += `<text class="bar-label" x="${lx + 15}" y="${y + 9}">${esc(name)}</text>`;
    s += `<text class="axis-num" x="${lx + lw}" y="${y + 9}" text-anchor="end" style="fill:var(--text-dim)">${pct.toFixed(1)}%</text>`;
    s += `<text class="axis-num" x="${lx + lw - 44}" y="${y + 9}" text-anchor="end">${fmtNum(src.count)}</text>`;
    s += `<rect x="${lx}" y="${y + 15}" width="${lw}" height="5" rx="2.5" fill="var(--border-soft)"/>`;
    s += `<rect x="${lx}" y="${y + 15}" width="${Math.max(3, (lw * pct) / 100).toFixed(1)}" height="5" rx="2.5" fill="${col}" opacity="0.85"/>`;
  });
  return s + `</svg>`;
}

/* ---------------- 主题语义网络 ---------------- */
function drawGraph(w, h, graph) {
  if (!graph || !graph.nodes || !graph.nodes.length) return null;
  const nodes = graph.nodes.map((nd) => ({ id: nd.topic, count: nd.count, label: nd.topic, x: 0, y: 0 }));
  const idx = {};
  nodes.forEach((n, i) => (idx[n.id] = i));
  const edges = (graph.edges || [])
    .map((e) => ({ a: idx[e.a], b: idx[e.b], w: e.w }))
    .filter((e) => e.a != null && e.b != null);
  const N = nodes.length;
  const cx = w / 2, cy = h / 2;
  nodes.forEach((n, i) => {
    const ang = (i / N) * Math.PI * 2 - Math.PI / 2;
    const r0 = Math.min(w, h) * 0.3;
    n.x = cx + r0 * Math.cos(ang);
    n.y = cy + r0 * Math.sin(ang);
  });

  /* Fruchterman-Reingold：斥力 k²/d、引力 d²/k、温度逐轮衰减。
     旧版是「常量斥力 + 固定步长」，节点会挤成一团（截图里标签全叠在一起）。
     k 随画布面积和节点数自适应，宽卡片自动摊开。 */
  const k = Math.sqrt((w * h) / Math.max(1, N)) * 0.66;
  let temp = Math.min(w, h) / 5;
  for (let it = 0; it < 300; it++) {
    const dx = new Array(N).fill(0), dy = new Array(N).fill(0);
    for (let i = 0; i < N; i++)
      for (let j = i + 1; j < N; j++) {
        let vx = nodes[i].x - nodes[j].x, vy = nodes[i].y - nodes[j].y;
        let d = Math.sqrt(vx * vx + vy * vy);
        if (d < 0.01) { vx = 0.01 * (i - j); vy = 0.01; d = 0.01; }
        const f = (k * k) / d;
        dx[i] += (vx / d) * f; dy[i] += (vy / d) * f;
        dx[j] -= (vx / d) * f; dy[j] -= (vy / d) * f;
      }
    edges.forEach((e) => {
      const vx = nodes[e.b].x - nodes[e.a].x, vy = nodes[e.b].y - nodes[e.a].y;
      const d = Math.max(0.01, Math.sqrt(vx * vx + vy * vy));
      const f = (d * d) / k;
      dx[e.a] += (vx / d) * f; dy[e.a] += (vy / d) * f;
      dx[e.b] -= (vx / d) * f; dy[e.b] -= (vy / d) * f;
    });
    nodes.forEach((n, i) => {
      dx[i] += (cx - n.x) * 0.12; dy[i] += (cy - n.y) * 0.12;
      const len = Math.max(0.01, Math.hypot(dx[i], dy[i]));
      const step = Math.min(len, temp);
      n.x += (dx[i] / len) * step;
      n.y += (dy[i] / len) * step;
    });
    temp *= 0.985;
  }

  const maxc = Math.max(...nodes.map((n) => n.count), 1);
  const rOf = (c) => 5 + Math.sqrt(c / maxc) * Math.min(17, Math.min(w, h) * 0.05);

  /* 布局归一化：把整团节点等比缩放、平移到画布内。
     只靠坐标钳制的话，力会把节点压到四条边上（截图里节点贴着卡片顶边被切掉），
     归一化之后无论几个方向、卡片多宽，都能刚好铺满中间区域。 */
  const padX = 74, padY = 40;
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  nodes.forEach((n) => {
    const r = rOf(n.count);
    minX = Math.min(minX, n.x - r); maxX = Math.max(maxX, n.x + r);
    minY = Math.min(minY, n.y - r); maxY = Math.max(maxY, n.y + r);
  });
  const spanX = Math.max(1, maxX - minX), spanY = Math.max(1, maxY - minY);
  const scale = Math.min((w - padX * 2) / spanX, (h - padY * 2) / spanY, 1.45);
  const offX = cx - ((minX + maxX) / 2) * scale;
  const offY = cy - ((minY + maxY) / 2) * scale;
  nodes.forEach((n) => {
    n.x = n.x * scale + offX;
    n.y = n.y * scale + offY;
  });
  /* 去重叠：力导向收敛后仍会有两个圆点叠在一起（尤其是同一簇里度数高的），
     这里补几轮「硬推开」，保证圆点之间至少留 8px 呼吸空间。 */
  for (let it = 0; it < 24; it++) {
    let moved = 0;
    for (let i = 0; i < N; i++)
      for (let j = i + 1; j < N; j++) {
        const ri = rOf(nodes[i].count), rj = rOf(nodes[j].count);
        const need = ri + rj + 8;
        let vx = nodes[j].x - nodes[i].x, vy = nodes[j].y - nodes[i].y;
        let d = Math.sqrt(vx * vx + vy * vy);
        if (d < 0.01) { vx = 0.01; vy = 0.01; d = 0.01; }
        if (d >= need) continue;
        const push = (need - d) / 2;
        nodes[i].x -= (vx / d) * push; nodes[i].y -= (vy / d) * push;
        nodes[j].x += (vx / d) * push; nodes[j].y += (vy / d) * push;
        moved++;
      }
    if (!moved) break;
  }
  nodes.forEach((n) => {
    const r = rOf(n.count);
    n.x = Math.max(padX - 30 + r, Math.min(w - padX + 30 - r, n.x));
    n.y = Math.max(padY - 16 + r, Math.min(h - padY + 16 - r, n.y));
  });

  let s = `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" class="graph-svg" role="img" aria-label="主题语义网络">`;
  edges.forEach((e) => {
    const a = nodes[e.a], b = nodes[e.b];
    const ecol = e.w >= 0.6 ? "var(--green)" : e.w >= 0.45 ? "var(--accent)" : "var(--border-strong)";
    s += `<line class="gedge" data-a="${e.a}" data-b="${e.b}" x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}" x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}" stroke="${ecol}" stroke-width="${(0.5 + e.w * 3).toFixed(2)}" opacity="${(0.18 + e.w * 0.5).toFixed(2)}"/>`;
  });
  nodes.forEach((n, i) => {
    const r = rOf(n.count);
    const col = AN_PALETTE[i % AN_PALETTE.length];
    s += `<g class="gnode" data-i="${i}"><circle class="gdot" cx="${n.x.toFixed(1)}" cy="${n.y.toFixed(1)}" r="${r.toFixed(1)}" fill="${col}" opacity="0.85" stroke="#0a0f16" stroke-width="1.5"><title>${esc(n.label)}：${n.count} 条知识点</title></circle></g>`;
  });

  /* 标签排布：大节点优先，依次尝试「下 / 上 / 右 / 左」四个方位，
     同时避开其他标签和其他节点——旧版只看标签之间是否重叠，
     结果标签常常压住隔壁的圆点。 */
  const placed = [];
  const hitBox = (a, b) => !(a.x + a.w < b.x || a.x > b.x + b.w || a.y + a.h < b.y || a.y > b.y + b.h);
  const hitNode = (box, self) =>
    nodes.some((n, j) => {
      if (j === self) return false;
      const r = rOf(n.count) + 2;
      const px = Math.max(box.x, Math.min(n.x, box.x + box.w));
      const py = Math.max(box.y, Math.min(n.y, box.y + box.h));
      return (px - n.x) ** 2 + (py - n.y) ** 2 < r * r;
    });
  const order = nodes.map((n, i) => i).sort((a, b) => nodes[b].count - nodes[a].count);
  const labels = [];
  order.forEach((i) => {
    const n = nodes[i];
    const r = rOf(n.count);
    const raw = n.label.length > 9 ? n.label.slice(0, 9) + "…" : n.label;
    const tw = textW(raw, 10.5) + 8;
    const th = 14;
    const tries = [
      { x: n.x - tw / 2, y: n.y + r + 4 },
      { x: n.x - tw / 2, y: n.y - r - th - 2 },
      { x: n.x + r + 6, y: n.y - th / 2 },
      { x: n.x - r - tw - 6, y: n.y - th / 2 },
    ];
    const box = tries.find(
      (b) =>
        b.x >= 0 && b.y >= 0 && b.x + tw <= w && b.y + th <= h &&
        !placed.some((p) => hitBox({ ...b, w: tw, h: th }, p)) &&
        !hitNode({ ...b, w: tw, h: th }, i)
    );
    if (!box) return;
    placed.push({ ...box, w: tw, h: th });
    labels.push({ i, x: box.x + tw / 2, y: box.y + th - 4, bx: box.x, by: box.y, tw, th, text: raw });
  });
  /* 标签衬底：原来用 3px 深色描边（paint-order:stroke）当衬底，可 10.5px 的
     中文字形笔画只有 1px 左右，描边有一半压在字形内侧，字被糊成一片。
     改成深色底板 + 浅色字：小字号也清清楚楚，还不挑背景。 */
  labels.forEach((l) => {
    s += `<rect class="node-label-bg" data-i="${l.i}" x="${l.bx.toFixed(1)}" y="${l.by.toFixed(1)}" width="${l.tw.toFixed(1)}" height="${l.th}" rx="4"/>`;
    s += `<text class="node-label" data-i="${l.i}" x="${l.x.toFixed(1)}" y="${l.y.toFixed(1)}" text-anchor="middle">${esc(l.text)}</text>`;
  });
  return s + `</svg>`;
}
/* 主题语义网络：装配 + 交互（拖拽 / 悬停高亮 / 点击展开该方向知识点）。

drawGraph 仍是纯渲染（可单测）；这里负责注入后绑定指针事件。
空库时给一段引导，指回「自主进化」——而不是丢一句「暂无数据」。
*/
let _graphSeq = 0;
let _graphData = null;

async function mountGraph(graph) {
  const box = $("#an-graph");
  const detail = $("#an-graph-detail");
  if (!box) return;
  const mySeq = ++_graphSeq;
  if (!graph || !graph.nodes || !graph.nodes.length) {
    _graphData = null;
    setHTML(box, `<div class="empty" style="text-align:left;line-height:1.8;padding:6px 2px">
      <div style="font-weight:600;margin-bottom:4px">知识网络需要至少两个学习方向</div>
      <div>去「自主进化」定一个具体的方向开始学习；学到两三个方向后回来，这里会显示它们之间的知识关联——连线越粗，两个方向的知识点越接近。</div>
      <button class="btn primary sm" data-goto="learn" style="margin-top:10px">去定方向</button>
    </div>`);
    if (detail) setHTML(detail, "");
    return;
  }
  _graphData = graph;
  const draw = (w, h) => drawGraph(w, h, graph) || emptyChart("暂无数据", h);
  chartFrame(box, 286, draw);
  if (detail) setHTML(detail, "悬停一个方向看它的关联，点一下看这个方向最近学到的知识");
  bindGraphInteractions(box, graph, mySeq, detail);
}

function bindGraphInteractions(box, graph, mySeq, detail) {
  const svg = box.querySelector("svg.graph-svg");
  if (!svg) return;
  const nodes = Array.from(svg.querySelectorAll("g.gnode"));
  const edges = Array.from(svg.querySelectorAll("line.gedge"));
  const N = nodes.length;
  const adj = Array.from({ length: N }, () => new Set());
  edges.forEach((el) => {
    const a = +el.dataset.a, b = +el.dataset.b;
    if (a < N && b < N) {
      adj[a].add(b);
      adj[b].add(a);
    }
  });

  // 邻居集合 → 高亮：命中的一圈满不透明，其余淡出
  function highlight(i) {
    const keep = i == null ? null : new Set([i, ...adj[i]]);
    nodes.forEach((g, j) => {
      const on = keep == null || keep.has(j);
      g.style.opacity = on ? "1" : "0.14";
    });
    edges.forEach((el) => {
      const a = +el.dataset.a, b = +el.dataset.b;
      const on = keep == null || (i != null && (a === i || b === i));
      el.style.opacity = on ? "0.95" : "0.05";
    });
  }

  function toLocal(evt) {
    const pt = svg.createSVGPoint();
    pt.x = evt.clientX;
    pt.y = evt.clientY;
    const m = svg.getScreenCTM();
    return m ? pt.matrixTransform(m.inverse()) : { x: 0, y: 0 };
  }

  // 拖拽某个节点时，同步更新它和所有关联边的端点
  function moveNode(i, x, y) {
    const g = nodes[i];
    const dot = g.querySelector("circle.gdot");
    if (!dot) return;
    dot.setAttribute("cx", x.toFixed(1));
    dot.setAttribute("cy", y.toFixed(1));
    // 标签已经拆成独立元素（为了做避让），按 data-i 找回来跟着走
    const svg = g.ownerSVGElement;
    const lab = svg && svg.querySelector(`text.node-label[data-i="${i}"]`);
    const labBg = svg && svg.querySelector(`rect.node-label-bg[data-i="${i}"]`);
    if (lab) {
      lab.setAttribute("x", x.toFixed(1));
      lab.setAttribute("y", (y + (+dot.getAttribute("r")) + 14).toFixed(1));
    }
    if (labBg) {
      const bw = +labBg.getAttribute("width") || 0;
      labBg.setAttribute("x", (x - bw / 2).toFixed(1));
      labBg.setAttribute("y", (y + (+dot.getAttribute("r")) + 4).toFixed(1));
    }
    edges.forEach((el) => {
      const a = +el.dataset.a, b = +el.dataset.b;
      if (a === i) {
        el.setAttribute("x1", x.toFixed(1));
        el.setAttribute("y1", y.toFixed(1));
      } else if (b === i) {
        el.setAttribute("x2", x.toFixed(1));
        el.setAttribute("y2", y.toFixed(1));
      }
    });
  }

  nodes.forEach((g, i) => {
    const dot = g.querySelector("circle.gdot");
    if (!dot) return;
    dot.style.cursor = "pointer";
    let sx = 0, sy = 0, moved = 0, dragging = false;
    dot.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      dragging = true;
      moved = 0;
      const p = toLocal(e);
      sx = p.x;
      sy = p.y;
      dot.setPointerCapture(e.pointerId);
      highlight(i);
    });
    dot.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      const p = toLocal(e);
      const dx = p.x - sx, dy = p.y - sy;
      moved = Math.max(moved, Math.hypot(dx, dy));
      const cx = +dot.getAttribute("cx"), cy = +dot.getAttribute("cy");
      moveNode(i, cx + dx, cy + dy);
      sx = p.x;
      sy = p.y;
    });
    dot.addEventListener("pointerup", (e) => {
      if (!dragging) return;
      dragging = false;
      dot.releasePointerCapture(e.pointerId);
      if (moved < 4) showTopic(i); // 位移很小才算点击
    });
    dot.addEventListener("pointercancel", () => {
      dragging = false;
      highlight(null);
    });
    g.addEventListener("mouseenter", () => {
      if (detail && mySeq === _graphSeq) {
        const nb = [...adj[i]];
        const t = g.querySelector("title");
        const label = t ? t.textContent.split("：")[0] : `方向 ${i + 1}`;
        setHTML(
          detail,
          nb.length
            ? `${esc(label)}：与 ${nb.map((j) => esc(graph.nodes[j].topic)).join("、")} 关联`
            : `${esc(label)}：暂无关联方向（学得越深，连线越多）`
        );
      }
      highlight(i);
    });
    g.addEventListener("mouseleave", () => highlight(null));
  });

  // 点击节点：拉该方向最近的知识点，就地展开
  async function showTopic(i) {
    if (!detail || mySeq !== _graphSeq) return;
    const topic = graph.nodes[i].topic;
    setHTML(detail, `正在加载「${esc(topic)}」的知识点…`);
    let data;
    try {
      data = await api(`/api/knowledge?topic=${encodeURIComponent(topic)}&limit=8`);
    } catch (e) {
      setHTML(detail, `加载失败：${esc(e.message || "网络错误")}`);
      return;
    }
    if (mySeq !== _graphSeq) return; // 已被新一轮渲染取代
    const items = (data && data.items) || [];
    if (!items.length) {
      setHTML(detail, `「${esc(topic)}」还没有知识点`);
      return;
    }
    const rows = items
      .map(
        (p) =>
          `<div style="display:flex;gap:8px;align-items:baseline;padding:2px 0">
            <span style="color:var(--accent);flex:0 0 auto">·</span>
            <span style="flex:1">${esc((p.claim || "").slice(0, 60))}</span>
            <span class="dim" style="flex:0 0 auto;font-size:11px">${p.confidence ? p.confidence.toFixed(2) : ""}</span>
          </div>`
      )
      .join("");
    setHTML(
      detail,
      `<div style="font-weight:600;margin:2px 0 4px">「${esc(topic)}」最近 ${items.length} 条</div>${rows}`
    );
  }
}

/* ---------------- 仪表盘 / 迷你折线 / 磁盘 ---------------- */
function drawGauge(size, rate, label) {
  rate = Math.max(0, Math.min(100, Math.round(rate) || 0));
  const cx = size / 2, cy = size / 2;
  const sw = Math.max(8, Math.round(size * 0.085));
  const r = size / 2 - sw / 2 - 3;
  const C = 2 * Math.PI * r;
  const off = C * (1 - rate / 100);
  const col = rate >= 70 ? "var(--green)" : rate >= 40 ? "var(--accent)" : "var(--amber)";
  return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
    <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="var(--border-soft)" stroke-width="${sw}"/>
    <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${col}" stroke-width="${sw}" stroke-linecap="round"
      stroke-dasharray="${C.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}" transform="rotate(-90 ${cx} ${cy})"/>
    <text x="${cx}" y="${cy + 2}" text-anchor="middle" style="font-family:var(--mono);font-size:${Math.round(size * 0.2)}px;font-weight:700;fill:var(--text)">${rate}%</text>
    <text x="${cx}" y="${cy + Math.round(size * 0.19)}" text-anchor="middle" class="axis-text">${esc(label || "命中率")}</text>
  </svg>`;
}

function drawSpark(w, h, arr) {
  if (!arr || arr.length === 0) return emptyChart("等待采样…", h);
  const padX = 3, padY = 8;
  const max = Math.max(...arr), min = Math.min(...arr);
  const span = Math.max(max - min, 1e-6);
  const n = arr.length;
  const x = (i) => (n === 1 ? w / 2 : padX + (i / (n - 1)) * (w - 2 * padX));
  const y = (v) => h - padY - ((v - min) / span) * (h - 2 * padY);
  let line = "";
  arr.forEach((v, i) => {
    line += (i === 0 ? "M" : "L") + x(i).toFixed(1) + " " + y(v).toFixed(1) + " ";
  });
  const area = line + `L ${x(n - 1).toFixed(1)} ${h - padY} L ${x(0).toFixed(1)} ${h - padY} Z`;
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">
    <defs><linearGradient id="spark-${Math.round(w)}-${arr.length}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.28"/>
      <stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/>
    </linearGradient></defs>
    <path d="${area}" fill="url(#spark-${Math.round(w)}-${arr.length})"/>
    <path d="${line}" fill="none" stroke="var(--accent)" stroke-width="1.6" stroke-linejoin="round"/>
    <circle cx="${x(n - 1).toFixed(1)}" cy="${y(arr[n - 1]).toFixed(1)}" r="2.6" fill="var(--accent)"/>
  </svg>`;
}

function drawDisk(w, h, d) {
  if (!d) return emptyChart("等待状态数据…", h);
  const used = +(d.used_gb || 0), budget = +(d.budget_gb || 0), free = +(d.free_gb || 0);
  const models = d.models || 0, datasets = d.datasets || 0;
  // 分母取预算；预算未配置(0)时退化为已用量，避免除零
  const denom = budget > 0 ? budget : Math.max(used, 1);
  const pct = Math.max(0, (used / denom) * 100);
  const col = pct >= 100 ? "var(--red)" : pct >= 90 ? "var(--amber)" : "var(--green)";
  const bh = 6;
  const y = 26;
  const wUsed = (Math.min(100, pct) / 100) * w;
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">
    <text class="axis-text" x="0" y="10">磁盘预算占用（模型 + 数据集）</text>
    <text class="axis-num" x="${w}" y="10" text-anchor="end" style="fill:var(--text-dim)">${used} / ${budget || "—"} GB · ${pct.toFixed(1)}%</text>
    <rect x="0" y="${y}" width="${w}" height="${bh}" rx="3" fill="var(--border-soft)"/>
    <rect x="0" y="${y}" width="${Math.max(2, wUsed).toFixed(1)}" height="${bh}" rx="3" fill="${col}"/>
    <text class="axis-text" x="0" y="${y + bh + 15}">磁盘剩余 ${free} GB · 模型 ${models} 个 · 数据集 ${datasets} 个</text>
  </svg>`;
}

/* ---------------- 学习历程 ---------------- */
function drawSessions(w, sd) {
  const items = (sd && sd.sessions) || [];
  if (!items.length)
    return { svg: emptyChart("还没有学习记录——去「资源获取」启动一轮，这里会画出每次任务的产出", 150), h: 150 };
  const list = items.slice(-8);
  const rowH = 38;
  const padT = 6;
  const H = padT * 2 + list.length * rowH;
  const labW = Math.max(96, Math.min(200, w * 0.24));
  const cntW = 54;   // 「84 条」列
  const metaW = 122; // 「6 轮 · 单轮 14」列
  const barX = labW + 14;
  const barMax = Math.max(30, w - barX - cntW - metaW - 6);
  const maxP = Math.max(...list.map((s) => s.new_points), 1);
  const colOf = (st) =>
    st === "done" ? "var(--green)" :
    st === "stopped" ? "var(--amber)" :
    st === "error" ? "var(--red)" : "var(--accent)";
  const fit = (txt, max) => {
    if (textW(txt, 11.5) <= max) return txt;
    let t = txt;
    while (t.length > 1 && textW(t + "…", 11.5) > max) t = t.slice(0, -1);
    return t + "…";
  };
  let s = `<svg width="${w}" height="${H}" viewBox="0 0 ${w} ${H}" role="img" aria-label="每次学习任务的产出">`;
  list.forEach((it, i) => {
    const y = padT + i * rowH;
    const cy = y + rowH / 2;
    const wBar = Math.max(3, (it.new_points / maxP) * barMax);
    const col = colOf(it.status);
    s += `<g class="sess-row">`;
    s += `<rect class="sess-bg" x="-8" y="${y + 1}" width="${w + 16}" height="${rowH - 4}" rx="7"/>`;
    s += `<circle cx="4" cy="${cy}" r="3" fill="${col}"/>`;
    s += `<text class="bar-label" x="14" y="${cy + 4}">${esc(fit(it.direction || "未命名方向", labW - 18))}</text>`;
    // 轨道压到几乎看不见：它只用来暗示「满格有多长」，不该比数据条更抢眼
    s += `<rect x="${barX}" y="${cy - 6}" width="${barMax}" height="12" rx="6" fill="#0b1119"/>`;
    s += `<rect x="${barX}" y="${cy - 6}" width="${wBar.toFixed(1)}" height="12" rx="6" fill="${col}" opacity="0.9"><title>新增 ${it.new_points} 条 · ${it.rounds} 轮 · 读 ${it.fetched} 篇 · 耗时 ${it.duration_s}s · 单轮 ${it.per_round} 条</title></rect>`;
    // 数值列左对齐到固定位置，扫一眼能纵向比较
    s += `<text class="axis-num" x="${(barX + barMax + 10).toFixed(1)}" y="${cy + 4}" style="fill:var(--text-dim)">${it.new_points} 条</text>`;
    s += `<text class="axis-num" x="${w - 2}" y="${cy + 4}" text-anchor="end">${it.rounds} 轮 · 单轮 ${it.per_round}</text>`;
    s += `</g>`;
  });
  return { svg: s + `</svg>`, h: H };
}

/* 缓存命中构成：一条 100% 堆叠条（命中 / 未命中）。
   检索平均耗时的迷你折线要攒够 20 次采样才有形状，刚打开分析页只有 1 个点，
   画出来是个孤零零的圆点——换成这条「命中 vs 未命中」，第一帧就有信息量。 */
function drawHitSplit(w, h, p) {
  const calls = p.search_calls || 0;
  const hits = p.search_cache_hits || 0;
  const miss = Math.max(0, calls - hits);
  const rate = calls ? hits / calls : 0;
  const barH = 10, y = 30;
  const wHit = Math.max(0, Math.min(1, rate)) * w;
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">
    <circle cx="4" cy="8" r="3.5" fill="var(--green)"/>
    <text class="axis-text" x="13" y="11.5">缓存命中 ${fmtNum(hits)} 次</text>
    <text class="axis-text" x="${w}" y="11.5" text-anchor="end">未命中 ${fmtNum(miss)} 次</text>
    <rect x="0" y="${y}" width="${w}" height="${barH}" rx="${barH / 2}" fill="var(--border-soft)"/>
    <rect x="0" y="${y}" width="${wHit.toFixed(1)}" height="${barH}" rx="${barH / 2}" fill="var(--green)" opacity="0.85"/>
    <text class="axis-text" x="0" y="${y + barH + 16}">重复检索与轮询直接复用上次结果，不重复计算</text>
  </svg>`;
}

/* ---------------- 检索解释 ---------------- */
function drawSearchExplain(w, data) {
  if (!data || !data.hits || !data.hits.length)
    return { svg: emptyChart("输入一个问题，看它命中了哪些知识点、每条匹配度多少", 84), h: 84 };
  const hits = data.hits;
  const rowH = 44;
  const H = hits.length * rowH + 2;
  const rankW = 22;
  const scoreW = 48;
  const barMax = Math.max(40, w - rankW - scoreW - 8);
  const maxScore = Math.max(...hits.map((h) => h.score), 0.01);
  const fit = (txt, max) => {
    if (textW(txt, 11.5) <= max) return txt;
    let t = txt;
    while (t.length > 1 && textW(t + "…", 11.5) > max) t = t.slice(0, -1);
    return t + "…";
  };
  let s = `<svg width="${w}" height="${H}" viewBox="0 0 ${w} ${H}" role="img" aria-label="检索命中列表">`;
  hits.forEach((h, i) => {
    const y = i * rowH;
    const bw = Math.max(4, (h.score / maxScore) * barMax);
    // 名次 + 命中文本 + 匹配条 + 分数，四列对齐，扫一眼就知道谁最相关
    s += `<text class="axis-num" x="0" y="${y + 13}" style="fill:var(--text-faint)">${i + 1}</text>`;
    s += `<text class="bar-label" x="${rankW}" y="${y + 13}">${esc(fit(h.claim || "", barMax - 4))}</text>`;
    s += `<rect x="${rankW}" y="${y + 20}" width="${barMax}" height="4" rx="2" fill="var(--border-soft)" opacity="0.55"/>`;
    s += `<rect x="${rankW}" y="${y + 20}" width="${bw.toFixed(1)}" height="4" rx="2" fill="var(--accent)" opacity="0.9"/>`;
    s += `<text class="axis-num" x="${w}" y="${y + 13}" text-anchor="end" style="fill:var(--text);font-size:11.5px">${(+h.score).toFixed(3)}</text>`;
  });
  return { svg: s + `</svg>`, h: H };
}

function drawSearchGraph(w, h, data) {
  if (!data || !data.hits || !data.hits.length) return emptyChart("命中关系会画在这里", 60);
  const hits = data.hits;
  const cx = w / 2, cy = h / 2;
  /* 椭圆布局：卡片是宽扁的，用正圆会让两边空出一大片；
     rx/ry 分开取，宽卡片也能撑满。 */
  const rx = Math.max(70, Math.min(w * 0.3, 300));
  const ry = Math.max(56, Math.min(h * 0.34, 104));
  let s = `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">`;
  const pts = hits.map((h2, i) => {
    const ang = (i / hits.length) * Math.PI * 2 - Math.PI / 2;
    return { x: cx + rx * Math.cos(ang), y: cy + ry * Math.sin(ang), ang, h: h2 };
  });
  pts.forEach((p) => {
    s += `<line x1="${cx}" y1="${cy}" x2="${p.x.toFixed(1)}" y2="${p.y.toFixed(1)}" stroke="var(--green)" stroke-width="${(0.4 + p.h.score * 3).toFixed(2)}" opacity="0.4"/>`;
  });
  s += `<circle cx="${cx}" cy="${cy}" r="24" fill="var(--accent)" opacity="0.92"/>` +
    `<text x="${cx}" y="${cy + 4}" text-anchor="middle" style="fill:#fff;font-size:11px">查询</text>`;
  pts.forEach((p) => {
    const r = 6 + Math.min(13, p.h.score * 15);
    s += `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${r.toFixed(1)}" fill="var(--green)" opacity="0.85"><title>${esc(p.h.claim || "")}</title></circle>`;
    // 标签沿半径向外放，右半边左对齐、左半边右对齐，既不压连线也不会互相叠
    const right = Math.cos(p.ang) >= 0;
    const lx = p.x + (right ? r + 8 : -r - 8);
    const room = right ? w - lx - 4 : lx - 4;
    const lab = fitText(p.h.claim || "", room, 10.5);
    if (lab) {
      s += `<text class="node-label" x="${lx.toFixed(1)}" y="${(p.y + 3.5).toFixed(1)}" text-anchor="${right ? "start" : "end"}" style="paint-order:stroke;stroke:#080b11;stroke-width:3px;stroke-linejoin:round">${esc(lab)}</text>`;
    }
  });
  return s + `</svg>`;
}

/* 按像素宽度截断（比按字数截断靠谱：中英文混排宽度差一倍） */
function fitText(txt, maxW, size = 10.5) {
  if (maxW < 24) return "";
  if (textW(txt, size) <= maxW) return txt;
  let t = txt;
  while (t.length > 1 && textW(t + "…", size) > maxW) t = t.slice(0, -1);
  return t.length > 1 ? t + "…" : "";
}

/* 数据版本标签：让「缓存」看得见——版本号不动就说明库没有新变化，
   此时服务端直接复用聚合结果，前端也不重建图表。 */
function paintAnVer(d) {
  const el = $("#an-ver");
  if (!el) return;
  const txt = `数据版本 #${d.ver} · 库无变化时 6 秒同步一次，不重复计算`;
  if (el.dataset.v === String(d.ver)) return;
  el.dataset.v = String(d.ver);
  setHTML(el, txt);
}

async function loadAnalytics(force = false) {
  let d;
  try {
    d = await api("/api/analytics");
  } catch (e) {
    toast("分析数据获取失败：" + e.message, "err");
    return;
  }
  paintAnVer(d);
  // 版本门控：库没变过就跳过整轮图表重建。省的不只是 DOM——7 个图表串
  // （增长/热力图/分布/直方图/环形/历程/力导向图）每轮都要完整构建一遍
  // 才能拿去和现有内容比对，力导向图还要跑一遍布局模拟。
  if (!force && state.anVer === d.ver) return;
  state.anVer = d.ver;

  /* 空库时不要把 8 张空图表摊在页面上：直接换成一张引导卡，
     告诉用户数据从哪来、点哪里开始。 */
  const isEmpty = !d.total;
  const emptyEl = $("#an-empty");
  const bodyEl = $("#an-body");
  if (emptyEl) emptyEl.hidden = !isEmpty;
  if (bodyEl) bodyEl.hidden = isEmpty;
  if (isEmpty) {
    setHTML($("#an-range-chip"), "");
    return;
  }

  setHTML($("#an-total"), String(d.total || 0));
  setHTML($("#an-topics-n"), String((d.topics || []).length));
  const srcs = d.sources || [];
  setHTML($("#an-sources-n"), String(srcs.length));
  setHTML($("#an-sources-top"), srcs.length ? srcs[0].kind + " 最多" : "—");
  const conf = d.confidence || [];
  let avg = 0, totN = 0;
  conf.forEach((b) => {
    avg += b.n * ((b.lo + b.hi) / 2);
    totN += b.n;
  });
  setHTML($("#an-conf-avg"), totN ? (avg / totN).toFixed(2) : "0");

  const growth = d.growth || [];
  mountGrowth($("#an-growth"), growth);
  setHTML(
    $("#an-growth-range"),
    growth.length ? `${growth[0].day} → ${growth[growth.length - 1].day}` : ""
  );
  setHTML($("#an-range-chip"), growth.length ? `${growth.length} 天` : "");

  // 学习活跃度热力图（复用 growth 数据）
  mountHeatmap($("#an-heatmap"), $("#an-heat-side"), growth);
  setHTML($("#an-heat-range"), growth.length ? `近 ${growth.length} 天` : "");

  setHTML($("#an-topics"), htmlTopicBars(d.topics));
  chartFrame($("#an-conf"), 240, (w, h) => drawHist(w, h, conf));
  setHTML($("#an-conf-meta"), totN ? `均值 ${(avg / totN).toFixed(2)} · ${fmtNum(totN)} 条` : "");
  chartFrame($("#an-source"), 320, (w, h) => drawDonut(w, h, srcs));
  mountGraph(d.graph);

  // 学习历程：每次任务的产出条形
  const sd = d.sessions || {};
  const nSess = (sd.sessions || []).length;
  chartFrame($("#an-sessions"), () => 0, (w) => drawSessions(w, sd).svg);
  setHTML(
    $("#an-sessions-sum"),
    sd.total_sessions
      ? `${sd.total_sessions} 次任务 · 新增 ${sd.total_points} 条 · ${sd.total_rounds} 轮${nSess > 8 ? " · 仅显示最近 8 次" : ""}`
      : ""
  );
  // 检索性能卡片：命中率仪表 + 平均耗时迷你折线
  loadPerf();
}

async function loadPerf() {
  let p;
  try {
    p = await api("/api/perf");
  } catch (_) {
    return;
  }
  const rate = Math.round((p.search_cache_hit_rate || 0) * 100);
  setHTML($("#an-perf-hit"), fmtNum(p.search_cache_hits || 0));
  setHTML($("#an-perf-ms"), (p.search_avg_ms || 0).toFixed(2) + " ms");
  setHTML($("#an-perf-calls"), fmtNum(p.search_calls || 0));
  const gEl = $("#an-perf-gauge");
  if (gEl) setHTML(ensureChart(gEl), drawGauge(118, rate, "命中率"));
  chartFrame($("#an-perf-split"), 56, (w, h) => drawHitSplit(w, h, p));
  // 系统状态卡片：状态接口自身耗时 + LLM 服务 + 磁盘预算
  // （磁盘/LLM 数据取自 refreshAll 已经拉过的 /api/status，不额外发请求）
  if (!state.status) {
    // 直接从 #analytics 深链进来时状态还没拉过，这里补一次，否则仪表盘会显示 0%
    try {
      state.status = await api("/api/status");
    } catch (_) {}
  }
  loadSysStatus(p);
}

function loadSysStatus(p) {
  const st = state.status || {};
  const llm = st.llm || {};
  const disk = st.disk || {};
  const models = (llm.models || []).length;
  const statusMs = p.status_avg_ms || 0;
  setHTML($("#an-sys-ms"), statusMs.toFixed(2) + " ms");
  setHTML($("#an-sys-llm"), llm.healthy ? "正常" : "离线");
  if ($("#an-sys-llm")) $("#an-sys-llm").style.color = llm.healthy ? "var(--green)" : "var(--red)";
  setHTML($("#an-sys-models"), String(models));
  const budgetPct = disk.budget_gb ? Math.round(((disk.used_gb || 0) / disk.budget_gb) * 100) : 0;
  const sg = $("#an-sys-gauge");
  if (sg) setHTML(ensureChart(sg), drawGauge(118, budgetPct, "预算占用"));
  chartFrame($("#an-sys-disk"), 52, (w, h) => drawDisk(w, h, disk));
  chartFrame($("#an-sys-spark"), 46, (w, h) => drawSpark(w, h, (p.status_series || []).slice(-20)));
}

async function runSearchExplain() {
  const q = $("#an-q").value.trim();
  if (!q) {
    toast("先输入一个查询");
    return;
  }
  let d;
  try {
    d = await api("/api/analytics/search?q=" + encodeURIComponent(q) + "&k=8");
  } catch (e) {
    toast("检索失败：" + e.message, "err");
    return;
  }
  chartFrame($("#an-search"), () => 0, (w) => drawSearchExplain(w, d).svg);
  chartFrame($("#an-search-graph"), 260, (w, h) => drawSearchGraph(w, h, d));
}

$("#btn-an-search")?.addEventListener("click", runSearchExplain);
$("#an-q")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") runSearchExplain();
});

/* ---------------- 数据备份 / 恢复 ---------------- */
const fmtSize = (b) => {
  b = Number(b) || 0;
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return (i === 0 ? b.toFixed(0) : b.toFixed(1)) + " " + u[i];
};

async function loadBackups() {
  const box = $("#bak-list");
  if (!box) return;
  try {
    const d = await api("/api/backups");
    if ($("#bak-keep")) $("#bak-keep").textContent = d.keep;
    paintBackups(d.items || []);
  } catch (e) {
    box.innerHTML = '<div class="empty">读取备份列表失败：' + esc(e.message) + "</div>";
  }
}

function paintBackups(items) {
  const box = $("#bak-list");
  if (!box) return;
  if (!items.length) {
    box.innerHTML = '<div class="empty">还没有备份。点上面的「一键备份」即可生成第一份。</div>';
    return;
  }
  box.innerHTML = items
    .map((b) => {
      const t = b.created_iso
        ? esc(b.created_iso.replace("T", " "))
        : esc(new Date(b.created_at * 1000).toLocaleString());
      const c = b.counts || {};
      const meta = [
        fmtSize(b.size),
        "知识点 " + (c.knowledge ?? "—"),
        "书 " + (c.books ?? "—"),
        "OCR " + (c.ocr_pages ?? "—"),
      ].join(" · ");
      const tag = b.label ? `<span class="tag">${esc(b.label)}</span> ` : "";
      return `<div class="bak-row">
        <div class="bak-info">
          <div class="bak-name">${tag}${esc(b.name)}</div>
          <div class="bak-meta">${t} · ${esc(meta)}</div>
        </div>
        <a class="btn sm" href="/api/backups/download/${encodeURIComponent(b.name)}" download="${esc(b.name)}">下载</a>
        <button class="btn sm danger" data-bak-del="${esc(b.name)}">删除</button>
      </div>`;
    })
    .join("");
}

$("#btn-bak-export")?.addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const label = ($("#bak-label").value || "").trim();
  btn.disabled = true;
  btn.textContent = "打包中…";
  try {
    const r = await api("/api/backups/export", { method: "POST", body: { label } });
    toast(`已备份 ${r.name}（${fmtSize(r.size)}）`, "ok");
    $("#bak-label").value = "";
    loadBackups();
  } catch (err) {
    toast("备份失败：" + err.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "一键备份";
  }
});

$("#btn-bak-restore")?.addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const file = $("#bak-file").files && $("#bak-file").files[0];
  if (!file) {
    toast("先选择一个备份 zip 文件");
    return;
  }
  if (
    !confirm(
      `确定用「${file.name}」覆盖当前知识库吗？\n\n当前库会先自动另存一份（备注 pre-restore）再替换。`
    )
  )
    return;
  btn.disabled = true;
  btn.textContent = "恢复中…";
  try {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("confirm", "true");
    const r = await fetch("/api/backups/restore", { method: "POST", body: fd });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || "HTTP " + r.status);
    toast(
      `恢复完成：知识点 ${d.counts?.knowledge ?? "—"} 条 · 书 ${d.counts?.books ?? "—"} 本（旧库已存为 ${d.pre_backup}）`,
      "ok"
    );
    $("#bak-file").value = "";
    loadBackups();
  } catch (err) {
    toast("恢复失败：" + err.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "恢复";
  }
});

/* 删除备份（事件委托，列表是动态渲染的） */
document.addEventListener("click", (e) => {
  const del = e.target.closest("[data-bak-del]");
  if (!del) return;
  const name = del.dataset.bakDel;
  if (!confirm(`确定删除备份 ${name} 吗？`)) return;
  api("/api/backups/" + encodeURIComponent(name), { method: "DELETE" })
    .then(() => {
      toast("已删除 " + name, "ok");
      loadBackups();
    })
    .catch((err) => toast("删除失败：" + err.message, "err"));
});

/* ---------------- 多模态 ---------------- */
/* 这一页只做两件事：把 URL 变成知识、把媒体素材搜出来。
   解析细节（视觉模型 / 字幕 / 转写 / OCR）全在后端，前端只显示能力状态和进度。 */
const MM_KIND_CN = { image: "图片", audio: "音频", video: "视频", pdf: "PDF" };
const MM_KIND_ICO = { image: "▣", audio: "♪", video: "▶", pdf: "▤" };

/* 能力卡：可用绿点、不可用灰点，副标题直接给出「缺什么、怎么补」 */
function paintCap(vSel, sSel, ok, main, note) {
  const v = $(vSel);
  const s = $(sSel);
  if (!v || !s) return;
  v.innerHTML = `<span class="mm-dot ${ok ? "on" : "off"}"></span>${esc(main)}`;
  v.style.fontSize = "17px";
  s.textContent = note || "";
  s.title = note || "";
}

async function loadMediaCaps() {
  try {
    const c = await api("/api/media/status");
    paintCap("#mm-vision", "#mm-vision-s", c.vision.ok,
      c.vision.ok ? "就绪" : "缺失",
      c.vision.ok ? `模型 ${c.vision.model}` : c.vision.reason);
    paintCap("#mm-asr", "#mm-asr-s", c.asr.ok,
      c.asr.ok ? "就绪" : "缺失",
      c.asr.ok ? `faster-whisper · ${c.asr.model}` : c.asr.reason);
    paintCap("#mm-sub", "#mm-sub-s", c.subtitles.ok,
      c.subtitles.ok ? "就绪" : "缺失",
      c.subtitles.ok ? "YouTube / Bilibili 字幕可直接取" : c.subtitles.reason);
    const pdfOk = c.pdf.ok && c.ocr.ok;
    paintCap("#mm-pdf", "#mm-pdf-s", pdfOk,
      c.pdf.ok ? (c.ocr.ok ? "就绪" : "仅文本层") : "缺失",
      c.pdf.ok ? (c.ocr.ok ? "文本层 + 扫描件 OCR" : "扫描件无法识别（缺 OCR）") : c.pdf.reason);

    const tag = $("#mm-mode");
    if (tag) {
      tag.textContent = c.enabled ? `已开启 · ${(c.kinds || []).join(" / ")}` : "已在设置中关闭";
    }
    const hint = $("#mm-storage");
    if (hint && c.storage) {
      hint.textContent = `缓存 ${c.storage.files} 个文件 / ${c.storage.mb} MB`;
    }
  } catch (e) {
    toast("能力检测失败：" + e.message, "err");
  }
  loadBackfillTopics();
}

/* 补漏方向下拉：直接用知识库的主题列表，选空 = 自动取知识点最多的前 6 个 */
async function loadBackfillTopics() {
  const sel = $("#mm-bf-topic");
  if (!sel) return;
  const keep = sel.value;
  try {
    const r = await api("/api/knowledge?limit=1");
    const topics = (r.topics || []).map((t) => t.topic).filter(Boolean);
    sel.innerHTML =
      '<option value="">全部方向（知识点最多的前 6 个）</option>' +
      topics.map((t) => `<option value="${esc(t)}">${esc(t)}</option>`).join("");
    if (keep && topics.includes(keep)) sel.value = keep;
  } catch (_) {
    sel.innerHTML = '<option value="">全部方向（知识点最多的前 6 个）</option>';
  }
}

async function startBackfill() {
  const topic = ($("#mm-bf-topic").value || "").trim();
  const per = parseInt($("#mm-bf-per").value, 10) || 2;
  try {
    const r = await api("/api/media/backfill", {
      method: "POST",
      body: { topic: topic || null, per_topic: per },
    });
    const box = $("#media-stream");
    if (box) box.innerHTML = "";
    toast(
      topic ? `开始补漏「${topic}」` : `开始补漏 ${r.topics.length} 个方向`,
      "ok"
    );
  } catch (e) {
    toast("启动失败：" + e.message, "err");
  }
}

function renderMediaResults(items) {
  const box = $("#mm-results");
  if (!box) return;
  if (!items || !items.length) {
    box.innerHTML = '<div class="empty">没有结果。换个关键词再试；国内网络下 YouTube 需要 yt-dlp 可用。</div>';
    return;
  }
  box.innerHTML = items
    .map((it) => {
      const kind = it.kind || "image";
      const thumb = it.thumb
        ? `<img class="mm-thumb" src="${esc(it.thumb)}" alt="" loading="lazy"
             onerror="this.style.visibility='hidden'" />`
        : `<div class="mm-thumb-ph">${MM_KIND_ICO[kind] || "▣"}</div>`;
      return `<div class="row"><div class="mm-item">
        ${thumb}
        <div class="mm-body">
          <div class="mm-title">${esc(it.title || it.url)}</div>
          ${it.snippet ? `<div class="mm-snip">${esc(it.snippet)}</div>` : ""}
          <div class="meta">
            <span class="mm-kind ${esc(kind)}">${MM_KIND_CN[kind] || kind}</span>
            <span>${esc(it.source || "")}</span>
            ${it.license ? `<span>${esc(String(it.license).slice(0, 28))}</span>` : ""}
          </div>
        </div>
        <button class="btn sm primary mm-act" data-mm-ingest="${esc(it.url)}">摄取</button>
      </div></div>`;
    })
    .join("");
}

async function startMediaIngest(urls) {
  const list = (Array.isArray(urls) ? urls : [urls]).map((s) => String(s || "").trim()).filter(Boolean);
  if (!list.length) {
    toast("先填一个链接", "err");
    return;
  }
  try {
    const r = await api("/api/media/ingest", {
      method: "POST",
      body: { urls: list, direction: ($("#mm-direction").value || "").trim() || null },
    });
    const box = $("#media-stream");
    if (box) box.innerHTML = "";
    toast(`已开始解析 ${r.count} 个来源，进度见右侧`, "ok");
  } catch (e) {
    toast("启动失败：" + e.message, "err");
  }
}

async function searchMedia() {
  const q = ($("#mm-q").value || "").trim();
  if (q.length < 2) {
    toast("关键词至少两个字符", "err");
    return;
  }
  const kinds = ["image", "audio", "video"].filter((k) => {
    const el = $("#mm-k-" + k);
    return el && el.checked;
  });
  if (!kinds.length) {
    toast("至少选一种素材类型", "err");
    return;
  }
  $("#mm-results").innerHTML = '<div class="empty">检索中…</div>';
  try {
    const r = await api("/api/media/search", { method: "POST", body: { q, kinds, limit: 6 } });
    renderMediaResults(r.items);
  } catch (e) {
    $("#mm-results").innerHTML = `<div class="empty">搜索失败：${esc(e.message)}</div>`;
  }
}

$("#btn-mm-ingest")?.addEventListener("click", () => {
  const urls = ($("#mm-urls").value || "").split(/\s*\n\s*/).filter(Boolean);
  startMediaIngest(urls);
});
$("#btn-mm-stop")?.addEventListener("click", async () => {
  try {
    await api("/api/learn/stop", { method: "POST" });
    toast("已请求停止，当前来源解析完就收尾", "");
  } catch (e) {
    toast("停止失败：" + e.message, "err");
  }
});
$("#btn-mm-search")?.addEventListener("click", searchMedia);
$("#btn-mm-backfill")?.addEventListener("click", startBackfill);
$("#mm-q")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") searchMedia();
});
/* 搜索结果里的「摄取」按钮是动态渲染的，用事件委托 */
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-mm-ingest]");
  if (!btn) return;
  startMediaIngest([btn.dataset.mmIngest]);
});

/* ---------------- 轮询 ---------------- */
let refreshBusy = false;
async function refreshAll() {
  if (refreshBusy || document.hidden) return;
  refreshBusy = true;
  try {
    const st = await api("/api/status");
    state.status = st;
    paintTopbar(st);
    if (st.auto) { autoOn = st.auto.enabled; paintAuto(st.auto); fillAutoSettings(st.auto); }
    if (st.config) fillRotationSettings(st.config);
    // 面板只在「自主进化」页可见：其他页面每 6 秒重建几十行子方向 DOM 是白费，
    // 切回该页时 loadRotation() 会补一次。
    if (st.rotation && state.view === "learn") paintRotation(st.rotation);
    await refreshSuggestions();
    if (state.view === "dashboard") { await loadDashboard(st); refreshHealth(); }
    // 知识库列表按写入版本门控：版本没变过就整轮跳过（服务端此时也命中缓存），
    // 不再每 6 秒无条件重拉 + 重建 60 行 DOM。
    if (state.view === "knowledge") {
      await loadRefinements();
      const ver = st.stats && st.stats.ver;
      if (ver !== state.kbVer) await loadKnowledge();
    }
    if (state.view === "analytics") {
      // 分析图按版本门控重建；但性能/系统采样必须每轮都更新，
      // 否则「缓存命中率 / 最近 20 次耗时」会一直停在第一次打开时的数值。
      const before = state.anVer;
      await loadAnalytics();
      if (state.anVer === before) loadPerf();
    }
  } catch (_) {}
  finally { refreshBusy = false; }
}

connectSSE();
switchView(location.hash.slice(1) || "dashboard", true);
// 首帧就把顶栏指标拉齐：只靠 6 秒轮询的话，深链直接进「分析」「书库」时
// 顶栏会空着显示 0，看起来像库是空的。放到下一个宏任务，让当前视图自己的
// 请求先发出去，别和它抢带宽。
setTimeout(refreshAll, 0);
setInterval(refreshAll, 6000);
maybeShowOnboarding();
