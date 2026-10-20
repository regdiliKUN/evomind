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
    try { msg = (await r.json()).detail || msg; } catch (_) {}
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
  resources: "资源获取",
  finetune: "权重进化",
  chat: "检验所学",
  settings: "设置",
};

function switchView(v, fromHash) {
  if (!TITLES[v]) v = "dashboard";
  state.view = v;
  if (!fromHash && location.hash.slice(1) !== v) location.hash = v;
  $$(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.view === v));
  $$(".view").forEach((s) => s.classList.toggle("active", s.id === "view-" + v));
  $("#page-title").textContent = TITLES[v] || v;
  if (v === "knowledge") loadKnowledge();
  if (v === "analytics") loadAnalytics();
  if (v === "library") loadLibrary();
  if (v === "resources") loadResources();
  if (v === "finetune") loadFinetune();
  if (v === "dashboard") loadDashboard(null, true);
  if (v === "settings") fillSettings();
  if (v === "learn") loadAuto();
}

$$(".nav-item").forEach((n) => n.addEventListener("click", () => switchView(n.dataset.view)));
window.addEventListener("hashchange", () => switchView(location.hash.slice(1), true));

/* ---------------- 事件流 ---------------- */
const STAGE_TEXT = {
  idle: "空闲", starting: "启动中", init: "初始化", planning: "诊断缺口",
  searching: "检索中", reading: "阅读中", extracting: "提炼中",
  resource: "资源扫描", reflecting: "反思中", done: "完成", error: "出错",
  stopping: "停止中",
};

function pushEvent(evt, streamEl, animate = true) {
  const box = streamEl || $("#learn-stream");
  const empty = box.querySelector(".empty");
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
  const total = state.status?.engine?.total_rounds || 1;
  const pct = Math.min(1, round / total);
  const C = 163.4;
  $("#ring-fg").style.strokeDashoffset = String(C * (1 - pct));
  $("#round-label").textContent = `第 ${round} / ${total} 轮`;
}

function connectSSE() {
  const es = new EventSource("/api/events");
  // 服务端回放历史后会发 history-end，之前收到的都算历史，不播入场动画
  let sseReady = false;
  let firstMsg = true;
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
      state.events.push(evt);
      pushEvent(evt, null, sseReady);
      if (evt.level === "download" || evt.level === "error") {
        if (evt.msg.includes("下载完成")) toast(evt.msg, "ok");
      }
    } catch (_) {}
  };
  es.onerror = () => {
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
  await syncDashEvents(forceEvents);
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
      box.innerHTML = '<div class="empty">暂无活动</div>';
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

function renderNextSteps(st) {
  const s = st.stats;
  const items = [];
  const add = (icon, title, desc, view, btn) =>
    items.push({ icon, title, desc, view, btn });

  // 规模提醒放最前面：这是性能问题，比「继续学」更该先处理
  if (s.scale_warn) {
    add("!", "知识库规模提醒", s.scale_warn, "knowledge", "去整理");
  }

  if (s.points === 0) {
    add("◐", "先让它学一个方向", "去「自主进化」填一个具体方向，点开始。3 轮约 10 分钟就有第一批知识。", "learn", "去学习");
  } else if (s.points < 30) {
    add("◑", "知识还比较薄", `目前 ${s.points} 条，建议再跑几轮加深，或换个角度补盲区。`, "learn", "继续学");
  } else if (s.points < 120) {
    add("◕", "可以检验一下学得怎么样", "去「检验所学」问几个真问题。答不上来的地方，就是下一步该补的方向。", "chat", "去提问");
  } else {
    add("●", "素材够了，可以试试权重进化", `已积累 ${s.points} 条知识。去「权重进化」把它们压进模型参数，得到真正「学会」的版本。`, "finetune", "去微调");
  }

  if (!st.finetune.ready) {
    add("⚙", "微调环境还没装", "想用权重层进化，需要先装 PyTorch 等依赖（约 3GB，装一次）。", "finetune", "去安装");
  }
  if (s.resources === 0 && st.config.auto_download) {
    add("⤓", "还没有自主下载过资源", "学习时它会自动扫描对口的模型和数据集，预算内自己下。也可以手动指定。", "resources", "看看");
  }
  add("✎", "随时可以问它", "它只用学过的知识回答并标出处，不会拿模型记忆糊弄你。", "chat", "去问");

  const html = items
    .slice(0, 3)
    .map(
      (i) => `<div class="row" style="display:flex;gap:14px;align-items:flex-start">
        <div style="font-size:19px;opacity:.65;line-height:1.3">${i.icon}</div>
        <div style="flex:1">
          <div style="font-size:13.8px;font-weight:600">${esc(i.title)}</div>
          <div class="detail" style="margin-top:4px">${esc(i.desc)}</div>
        </div>
        <button class="btn sm" data-goto="${i.view}">${esc(i.btn)}</button>
      </div>`
    )
    .join("");

  // 内容没变就不重建，避免按钮每 6 秒闪一下
  if (!setHTML($("#next-steps"), html)) return;

  $$("#next-steps [data-goto]").forEach((b) =>
    b.addEventListener("click", () => switchView(b.dataset.goto))
  );
}

function paintTopbar(st) {
  $("#m-points").textContent = st.stats.points;
  $("#m-docs").textContent = st.stats.documents;
  $("#m-disk").textContent = st.stats.disk_gb + " GB";
  const running = st.engine.running;
  $("#m-state").textContent = running ? "学习中" : "空闲";
  $("#m-state").style.color = running ? "var(--green)" : "";

  const llmOk = st.llm.healthy;
  $("#dot-llm").className = "dot " + (llmOk ? "on" : "off");
  $("#txt-llm").textContent = llmOk ? "模型在线" : "模型离线";

  $("#dot-gpu").className = "dot " + (st.finetune.cuda ? "on" : "warn");
  $("#txt-gpu").textContent = st.finetune.cuda
    ? (st.finetune.gpu || "").replace("NVIDIA GeForce ", "")
    : st.finetune.ready
      ? "CUDA 不可用"
      : "微调未装";

  $("#btn-start").disabled = running;
  $("#btn-stop").disabled = !running;
  $("#stage-tag").textContent = STAGE_TEXT[st.engine.stage] || st.engine.stage;
  if (st.engine.running) updateRing(st.engine.round);
}

/* ---------------- 学习 ---------------- */
$("#btn-start").addEventListener("click", async () => {
  const direction = $("#direction").value.trim();
  if (!direction) return toast("先写一个学习方向", "err");
  const rounds = parseInt($("#rounds").value, 10);
  $("#learn-stream").innerHTML = "";
  try {
    const r = await api("/api/learn/start", { method: "POST", body: { direction, rounds } });
    if (!r.ok) return toast(r.msg || "启动失败", "err");
    toast("已启动进化学习", "ok");
    $("#btn-start").disabled = true;
    $("#btn-stop").disabled = false;
  } catch (e) {
    toast("启动失败：" + e.message, "err");
  }
});

$("#btn-stop").addEventListener("click", async () => {
  await api("/api/learn/stop", { method: "POST" });
  toast("已发送停止指令");
});

$$("#presets .chip").forEach((c) =>
  c.addEventListener("click", () => {
    $("#direction").value = c.textContent;
  })
);

/* ---------------- 持续自学 ---------------- */
let autoOn = false;

async function loadAuto() {
  try {
    const a = await api("/api/auto");
    autoOn = a.enabled;
    paintAuto(a);
  } catch (_) {}
}

function paintAuto(a) {
  const btn = $("#btn-auto");
  btn.textContent = a.enabled ? "已开启 · 点此关闭" : "开启";
  btn.className = "btn sm" + (a.enabled ? " primary" : "");
  const d = $("#auto-desc");
  if (a.enabled) {
    const idle = a.idle_minutes;
    d.innerHTML = `已开启：沿用「${esc(a.direction || "上次方向")}」，空闲 ${idle} 分钟后自动学 2 轮（已自动学 ${a.runs} 次）`;
    d.style.color = "var(--green)";
  } else {
    d.textContent = "开启后，学完一段空闲一会儿就自动接着学，不用一直盯着";
    d.style.color = "";
  }
}

$("#btn-auto").addEventListener("click", async () => {
  const direction = $("#direction").value.trim();
  if (!autoOn && !direction) {
    const last = state.status?.config?.last_direction;
    if (!last) return toast("先填一个学习方向，再开启持续自学", "err");
  }
  try {
    const r = await api("/api/auto", {
      method: "POST",
      body: { enabled: !autoOn, direction: direction || undefined },
    });
    autoOn = r.enabled;
    paintAuto(r);
    toast(r.enabled ? "持续自学已开启" : "持续自学已关闭", r.enabled ? "ok" : "");
  } catch (e) {
    toast(e.message, "err");
  }
});

/* ---------------- 知识库 ---------------- */
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
    if (!q) fillTopicSelect(r.items);
  } catch (e) {
    toast("知识库加载失败：" + e.message, "err");
  }
}

function fillTopicSelect(items) {
  const sel = $("#kb-topic");
  if (sel.dataset.filled === "1") return;
  const topics = [...new Set(items.map((i) => i.topic))];
  if (!topics.length) return;
  sel.innerHTML =
    '<option value="">全部方向</option>' +
    topics.map((t) => `<option value="${esc(t)}">${esc(t)}</option>`).join("");
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
      return `<div class="row">
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

$("#btn-kb-search").addEventListener("click", () => {
  const q = $("#kb-search").value.trim();
  loadKnowledge(q);
});
$("#kb-search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("#btn-kb-search").click();
});
$("#kb-topic").addEventListener("change", () => loadKnowledge());

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
  if (!dirs.length) {
    box.innerHTML = '<div class="empty">还没有添加目录</div>';
    return;
  }
  box.innerHTML = dirs
    .map(
      (d) => `<div class="row">
        <div class="rowflex" style="justify-content:space-between">
          <span style="font-size:13.5px;font-weight:600">${esc(d.label || d.path)}</span>
          <div class="rowflex" style="gap:6px">
            <span class="tagmini">${d.book_count} 个文件</span>
            <button class="btn sm" data-dir-scan="${d.id}">扫描</button>
            <button class="btn sm danger" data-dir-del="${d.id}">移除</button>
          </div>
        </div>
        <div class="meta">
          <span class="mono" style="font-size:11px">${esc(d.path)}</span>
          ${(d.excludes || []).length ? `<span class="tagmini">排除 ${esc(d.excludes.join("、"))}</span>` : ""}
          <span>已导入 ${d.imported_count} 本</span>
        </div>
      </div>`
    )
    .join("");

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
  try {
    const r = await api("/api/library/books?" + params);
    renderLibBooks(r.items, r.total);
  } catch (e) {
    toast("书目加载失败：" + e.message, "err");
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
      const canPick = ["pending", "ready", "skipped"].includes(b.status);
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
    if (r.ok) toast(`扫描完成：发现 ${r.found} 个，新增 ${r.added} 个`, "ok");
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
    $("#r-auto").textContent = state.cfg?.auto_download ? "开启" : "关闭";

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
      if (st.running && st.stage) $("#ft-ds").textContent = st.stage;
      if (!st.running) {
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
$("#chat-q").addEventListener("keydown", (e) => {
  if (e.key === "Enter") askChat();
});

/* ---------------- 设置 ---------------- */
async function fillSettings() {
  try {
    const [cfg, models] = await Promise.all([api("/api/config"), api("/api/models")]);
    state.cfg = cfg;
    $("#cfg-model").innerHTML = models
      .map(
        (m) =>
          `<option value="${esc(m.name)}" ${m.name === cfg.model ? "selected" : ""}>${esc(
            m.name
          )} · ${m.size_gb}GB ${m.caps?.includes("tools") ? "· 工具调用" : ""}</option>`
      )
      .join("");
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

/* ---------------- 分析视图 ---------------- */
/* 全部图表用纯 SVG 现画（不引第三方库，和前端「无框架」的基调一致）。
   主题色直接用 CSS 变量，换肤也跟着变。 */
const AN_PALETTE = [
  "var(--accent)", "var(--green)", "var(--amber)", "var(--purple)",
  "var(--red)", "#5ad0ff", "#ff9bd0", "#7be0a0",
];

function svgGrowth(growth) {
  if (!growth || !growth.length) return '<div class="empty">还没有知识增长数据</div>';
  const W = 680, H = 240, pl = 46, pr = 12, pt = 16, pb = 28;
  const pw = W - pl - pr, ph = H - pt - pb;
  const n = growth.length;
  const cmax = Math.max(...growth.map((d) => d.cumulative), 1);
  const amax = Math.max(...growth.map((d) => d.added), 1);
  const X = (i) => pl + (n === 1 ? pw / 2 : (i / (n - 1)) * pw);
  const Yc = (v) => pt + ph * (1 - v / cmax);
  const Ya = (v) => pt + ph * (1 - v / amax);
  let grid = "";
  for (let g = 0; g <= 4; g++) {
    const y = pt + (ph * g) / 4;
    const val = Math.round(cmax * (1 - g / 4));
    grid += `<line class="grid-line" x1="${pl}" y1="${y.toFixed(1)}" x2="${W - pr}" y2="${y.toFixed(1)}"/>` +
      `<text class="axis-text" x="${pl - 6}" y="${(y + 3).toFixed(1)}" text-anchor="end">${val}</text>`;
  }
  let line = "", area = "";
  if (n > 1) {
    const pts = growth.map((d, i) => [X(i), Yc(d.cumulative)]);
    line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
    area = `M${pts[0][0].toFixed(1)} ${(pt + ph).toFixed(1)} ` +
      pts.map((p) => "L" + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ") +
      ` L${pts[pts.length - 1][0].toFixed(1)} ${(pt + ph).toFixed(1)} Z`;
  }
  const bw = Math.max(2, (pw / n) * 0.5);
  let bars = "";
  growth.forEach((d, i) => {
    const x = X(i) - bw / 2;
    const y = Ya(d.added);
    bars += `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${(pt + ph - y).toFixed(1)}" rx="1.5" fill="var(--accent)" opacity="0.4"/>`;
  });
  const labI = n === 1 ? [0] : [0, Math.floor((n - 1) / 2), n - 1];
  let xl = "";
  labI.forEach((i) => {
    xl += `<text class="axis-text" x="${X(i).toFixed(1)}" y="${H - 8}" text-anchor="middle">${esc(growth[i].day.slice(5))}</text>`;
  });
  return `<svg viewBox="0 0 ${W} ${H}">` +
    `<defs><linearGradient id="grad-growth" x1="0" y1="0" x2="0" y2="1">` +
    `<stop offset="0%" stop-color="var(--green)" stop-opacity="0.35"/>` +
    `<stop offset="100%" stop-color="var(--green)" stop-opacity="0"/></linearGradient></defs>` +
    grid + (area ? `<path d="${area}" fill="url(#grad-growth)"/>` : "") +
    (line ? `<path d="${line}" fill="none" stroke="var(--green)" stroke-width="2"/>` : "") +
    bars + xl + `</svg>`;
}

function htmlTopicBars(topics) {
  if (!topics || !topics.length) return '<div class="empty">还没有任何学习记录</div>';
  const max = Math.max(...topics.map((t) => t.count));
  return topics.slice(0, 12).map((t) =>
    `<div style="margin-bottom:11px">
      <div class="rowflex" style="justify-content:space-between;font-size:12.5px">
        <span>${esc(t.topic)}</span>
        <span class="mono dim">${t.count} · 可信 ${(+t.avg_conf).toFixed(2)}</span>
      </div>
      <div class="bar"><i style="width:${(t.count / max * 100).toFixed(1)}%"></i></div>
    </div>`
  ).join("");
}

function svgHist(confidence) {
  if (!confidence || !confidence.length) return '<div class="empty">暂无数据</div>';
  const W = 680, H = 200, pl = 40, pr = 10, pt = 14, pb = 26;
  const pw = W - pl - pr, ph = H - pt - pb;
  const n = confidence.length;
  const maxn = Math.max(...confidence.map((b) => b.n), 1);
  const bw = pw / n;
  let s = "";
  for (let g = 0; g <= 4; g++) {
    const y = pt + (ph * g) / 4;
    const val = Math.round(maxn * (1 - g / 4));
    s += `<line class="grid-line" x1="${pl}" y1="${y.toFixed(1)}" x2="${W - pr}" y2="${y.toFixed(1)}"/>` +
      `<text class="axis-text" x="${pl - 5}" y="${(y + 3).toFixed(1)}" text-anchor="end">${val}</text>`;
  }
  confidence.forEach((b, i) => {
    const x = pl + i * bw, h = ph * (b.n / maxn), y = pt + ph - h;
    const t = n > 1 ? i / (n - 1) : 0;
    const col = t < 0.5 ? (t < 0.25 ? "var(--red)" : "var(--amber)") : (t < 0.75 ? "var(--green)" : "var(--accent)");
    s += `<rect x="${(x + 1).toFixed(1)}" y="${y.toFixed(1)}" width="${(bw - 2).toFixed(1)}" height="${h.toFixed(1)}" rx="2" fill="${col}" opacity="0.82"/>`;
  });
  [0, Math.floor((n - 1) / 2), n - 1].forEach((i) => {
    const x = pl + i * bw + bw / 2;
    s += `<text class="axis-text" x="${x.toFixed(1)}" y="${H - 8}" text-anchor="middle">${(+confidence[i].lo).toFixed(1)}–${(+confidence[i].hi).toFixed(1)}</text>`;
  });
  return `<svg viewBox="0 0 ${W} ${H}">${s}</svg>`;
}

function arcPath(cx, cy, rO, rI, a0, a1) {
  const p = (a, r) => [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  const [x0, y0] = p(a0, rO), [x1, y1] = p(a1, rO), [x2, y2] = p(a1, rI), [x3, y3] = p(a0, rI);
  const large = a1 - a0 > Math.PI ? 1 : 0;
  return `M${x0.toFixed(1)} ${y0.toFixed(1)} A${rO} ${rO} 0 ${large} 1 ${x1.toFixed(1)} ${y1.toFixed(1)} ` +
    `L${x2.toFixed(1)} ${y2.toFixed(1)} A${rI} ${rI} 0 ${large} 0 ${x3.toFixed(1)} ${y3.toFixed(1)} Z`;
}

function svgDonut(sources) {
  if (!sources || !sources.length) return '<div class="empty">暂无来源数据</div>';
  const total = sources.reduce((a, b) => a + b.count, 0) || 1;
  const cx = 110, cy = 110, rO = 80, rI = 48;
  let s = `<svg viewBox="0 0 680 220">`;
  let a0 = -Math.PI / 2;
  sources.forEach((src, i) => {
    const frac = src.count / total;
    if (frac <= 0.0005) return;
    const a1 = a0 + frac * Math.PI * 2 * 0.9999;
    const col = AN_PALETTE[i % AN_PALETTE.length];
    s += `<path d="${arcPath(cx, cy, rO, rI, a0, a1)}" fill="${col}" opacity="0.85"><title>${esc(src.kind)}: ${src.count}</title></path>`;
    a0 = a1;
  });
  s += `<text x="${cx}" y="${cy - 4}" text-anchor="middle" style="fill:var(--text);font-size:22px;font-weight:600">${total}</text>`;
  s += `<text x="${cx}" y="${cy + 14}" text-anchor="middle" class="axis-text">总条数</text>`;
  let ly = 30;
  sources.forEach((src, i) => {
    const col = AN_PALETTE[i % AN_PALETTE.length];
    const pct = ((src.count / total) * 100).toFixed(1);
    s += `<g transform="translate(230,${ly})"><rect width="11" height="11" rx="3" fill="${col}"/>` +
      `<text x="17" y="10" class="bar-label">${esc(src.kind)} · ${src.count} (${pct}%)</text></g>`;
    ly += 24;
  });
  return s + `</svg>`;
}

function svgGraph(graph) {
  if (!graph || !graph.nodes || !graph.nodes.length) return '<div class="empty">还没有可成网的知识点</div>';
  const nodes = graph.nodes.map((nd) => ({ id: nd.topic, count: nd.count, label: nd.topic, x: 0, y: 0 }));
  const idx = {};
  nodes.forEach((n, i) => (idx[n.id] = i));
  const edges = (graph.edges || [])
    .map((e) => ({ a: idx[e.a], b: idx[e.b], w: e.w }))
    .filter((e) => e.a != null && e.b != null);
  const N = nodes.length;
  const W = 680, H = 320, cx = W / 2, cy = H / 2;
  nodes.forEach((n, i) => {
    const ang = (i / N) * Math.PI * 2;
    n.x = cx + 110 * Math.cos(ang);
    n.y = cy + 110 * Math.sin(ang);
  });
  const rep = 9000, k = 220;
  for (let it = 0; it < 240; it++) {
    const fx = new Array(N).fill(0), fy = new Array(N).fill(0);
    for (let i = 0; i < N; i++)
      for (let j = i + 1; j < N; j++) {
        let dx = nodes[i].x - nodes[j].x, dy = nodes[i].y - nodes[j].y;
        const d2 = dx * dx + dy * dy + 0.01, d = Math.sqrt(d2);
        const f = rep / d2;
        fx[i] += (f * dx) / d; fy[i] += (f * dy) / d;
        fx[j] -= (f * dx) / d; fy[j] -= (f * dy) / d;
      }
    edges.forEach((e) => {
      let dx = nodes[e.b].x - nodes[e.a].x, dy = nodes[e.b].y - nodes[e.a].y;
      const d = Math.sqrt(dx * dx + dy * dy) + 0.01;
      const f = k * (d - 90);
      fx[e.a] += (f * dx) / d; fy[e.a] += (f * dy) / d;
      fx[e.b] -= (f * dx) / d; fy[e.b] -= (f * dy) / d;
    });
    nodes.forEach((n, i) => {
      fx[i] += (cx - n.x) * 0.02;
      fy[i] += (cy - n.y) * 0.02;
      n.x += Math.max(-8, Math.min(8, fx[i] * 0.05));
      n.y += Math.max(-8, Math.min(8, fy[i] * 0.05));
      n.x = Math.max(42, Math.min(W - 42, n.x));
      n.y = Math.max(28, Math.min(H - 28, n.y));
    });
  }
  const maxc = Math.max(...nodes.map((n) => n.count), 1);
  let s = `<svg viewBox="0 0 ${W} ${H}">`;
  edges.forEach((e) => {
    const a = nodes[e.a], b = nodes[e.b];
    // 边色按相似度分档：越近越偏绿，弱连接用中性色，一眼看出哪些方向关联最紧
    const ecol = e.w >= 0.6 ? "var(--green)" : e.w >= 0.45 ? "var(--accent)" : "var(--border-strong)";
    s += `<line x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}" x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}" stroke="${ecol}" stroke-width="${(0.4 + e.w * 4).toFixed(2)}" opacity="${(0.18 + e.w * 0.55).toFixed(2)}"/>`;
  });
  nodes.forEach((n, i) => {
    const r = 5 + Math.sqrt(n.count / maxc) * 16;
    const col = AN_PALETTE[i % AN_PALETTE.length];
    s += `<circle cx="${n.x.toFixed(1)}" cy="${n.y.toFixed(1)}" r="${r.toFixed(1)}" fill="${col}" opacity="0.82" stroke="#0a0f16" stroke-width="1.5"><title>${esc(n.label)}: ${n.count}</title></circle>`;
    if (N <= 14 || r > 11) {
      const lab = n.label.length > 10 ? n.label.slice(0, 10) + "…" : n.label;
      s += `<text class="node-label" x="${n.x.toFixed(1)}" y="${(n.y + r + 11).toFixed(1)}" text-anchor="middle">${esc(lab)}</text>`;
    }
  });
  return s + `</svg>`;
}

function svgGauge(rate) {
  rate = Math.max(0, Math.min(100, rate | 0));
  const W = 150, H = 150, cx = 75, cy = 75, r = 58;
  const C = 2 * Math.PI * r;
  const off = C * (1 - rate / 100);
  const col = rate >= 70 ? "var(--green)" : rate >= 40 ? "var(--accent)" : "var(--orange, #e0a23b)";
  return `<svg viewBox="0 0 ${W} ${H}">
    <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="var(--border-soft)" stroke-width="12"/>
    <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${col}" stroke-width="12" stroke-linecap="round"
      stroke-dasharray="${C.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}" transform="rotate(-90 ${cx} ${cy})"/>
    <text x="${cx}" y="${cy - 2}" text-anchor="middle" style="font-family:var(--mono);font-size:30px;font-weight:700;fill:var(--text)">${rate}%</text>
    <text x="${cx}" y="${cy + 20}" text-anchor="middle" class="axis-text">命中率</text>
  </svg>`;
}

function svgSpark(arr) {
  if (!arr || arr.length === 0) return '<div class="empty">等待采样…</div>';
  const W = 380, H = 54, pad = 8;
  const max = Math.max(...arr), min = Math.min(...arr);
  const span = Math.max(max - min, 1e-6);
  const n = arr.length;
  const x = (i) => (n === 1 ? W / 2 : pad + (i / (n - 1)) * (W - 2 * pad));
  const y = (v) => H - pad - ((v - min) / span) * (H - 2 * pad);
  let line = "";
  arr.forEach((v, i) => {
    line += (i === 0 ? "M" : "L") + x(i).toFixed(1) + " " + y(v).toFixed(1) + " ";
  });
  const area = line + `L ${x(n - 1).toFixed(1)} ${H - pad} L ${x(0).toFixed(1)} ${H - pad} Z`;
  const dots = arr.map((v, i) => `<circle cx="${x(i).toFixed(1)}" cy="${y(v).toFixed(1)}" r="2.2" fill="var(--accent)"/>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}">
    <path d="${area}" fill="var(--accent)" opacity="0.12"/>
    <path d="${line}" fill="none" stroke="var(--accent)" stroke-width="2"/>
    ${dots}
  </svg>`;
}

function renderSearchExplain(data) {
  if (!data || !data.hits || !data.hits.length) return '<div class="empty">没有命中任何知识点</div>';
  const W = 680, H = 10 + data.hits.length * 30;
  const maxScore = Math.max(...data.hits.map((h) => h.score), 0.01);
  let s = `<svg viewBox="0 0 ${W} ${H}">`;
  data.hits.forEach((h, i) => {
    const y = 10 + i * 30;
    const w = Math.max(2, (h.score / maxScore) * (W - 280));
    s += `<rect x="0" y="${y}" width="${w.toFixed(1)}" height="14" rx="3" fill="var(--accent)" opacity="0.8"/>`;
    s += `<text class="bar-label" x="${(w + 8).toFixed(1)}" y="${y + 11}">${esc((h.claim || "").slice(0, 30))} · ${(+h.score).toFixed(3)}</text>`;
  });
  return s + `</svg>`;
}

function svgSearchGraph(data) {
  if (!data || !data.hits || !data.hits.length) return "";
  const W = 680, H = 300, cx = W / 2, cy = H / 2;
  const hits = data.hits;
  let s = `<svg viewBox="0 0 ${W} ${H}">`;
  hits.forEach((h, i) => {
    const ang = (i / hits.length) * Math.PI * 2;
    const R = 110;
    h._x = cx + R * Math.cos(ang);
    h._y = cy + R * Math.sin(ang);
    s += `<line x1="${cx}" y1="${cy}" x2="${h._x.toFixed(1)}" y2="${h._y.toFixed(1)}" stroke="var(--green)" stroke-width="${(0.3 + h.score * 5).toFixed(2)}" opacity="0.6"/>`;
  });
  s += `<circle cx="${cx}" cy="${cy}" r="26" fill="var(--accent)" opacity="0.9"/>` +
    `<text x="${cx}" y="${cy + 4}" text-anchor="middle" class="node-label" style="fill:#fff">查询</text>`;
  hits.forEach((h) => {
    const r = 6 + Math.min(14, h.score * 16);
    s += `<circle cx="${h._x.toFixed(1)}" cy="${h._y.toFixed(1)}" r="${r.toFixed(1)}" fill="var(--green)" opacity="0.85"/>` +
      `<text class="node-label" x="${h._x.toFixed(1)}" y="${(h._y + r + 12).toFixed(1)}" text-anchor="middle">${esc((h.claim || "").slice(0, 8))}…</text>`;
  });
  return s + `</svg>`;
}

function svgHeatmap(growth) {
  if (!growth || !growth.length) return '<div class="empty">还没有学习记录</div>';
  const added = {};
  growth.forEach((g) => (added[g.day] = g.added));
  const days = growth.map((g) => g.day).sort();
  const first = new Date(days[0] + "T00:00:00Z");
  const last = new Date(days[days.length - 1] + "T00:00:00Z");
  // 对齐到起点所在周的周日，做成 GitHub 贡献式日历格
  const start = new Date(first);
  start.setUTCDate(first.getUTCDate() - first.getUTCDay());
  const maxAdd = Math.max(...growth.map((g) => g.added), 1);
  const cell = 13, gap = 3, left = 20, top = 18;
  const totalDays = Math.round((last - start) / 86400000) + 1;
  const weeks = Math.ceil(totalDays / 7);
  const W = left + weeks * (cell + gap) + 6;
  const H = top + 7 * (cell + gap) + 4;
  const styleOf = (n) => {
    if (n <= 0) return ["var(--border-soft)", 0.5];
    const r = n / maxAdd;
    if (r > 0.66) return ["var(--green)", 0.95];
    if (r > 0.33) return ["var(--accent)", 0.72];
    return ["var(--accent)", 0.45];
  };
  const WD = ["日", "一", "二", "三", "四", "五", "六"];
  let s = `<svg viewBox="0 0 ${W} ${H}">`;
  for (let d = 1; d < 7; d += 2)
    s += `<text class="axis-text" x="0" y="${top + d * (cell + gap) + cell - 3}">${WD[d]}</text>`;
  // 月份标签：某一周列跨入新月份时在顶部标注
  let lastMonth = -1;
  for (let wk = 0; wk < weeks; wk++) {
    const dt = new Date(start.getTime() + wk * 7 * 86400000);
    const m = dt.getUTCMonth();
    if (m !== lastMonth) {
      lastMonth = m;
      s += `<text class="axis-text" x="${left + wk * (cell + gap)}" y="11">${m + 1}月</text>`;
    }
  }
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
    s += `<rect x="${x}" y="${y}" width="${cell}" height="${cell}" rx="2.5" fill="${col}" opacity="${op}"><title>${key}：新增 ${n} 条</title></rect>`;
  }
  return s + `</svg>`;
}

function svgSessions(sd) {
  const items = (sd && sd.sessions) || [];
  if (!items.length)
    return '<div class="empty">还没有学习记录——去「资源获取」启动一轮，这里会画出每次任务的产出</div>';
  const W = 680, labW = 150, rowH = 46, padT = 10;
  const maxP = Math.max(...items.map((s) => s.new_points), 1);
  const H = padT * 2 + items.length * rowH;
  const barX = labW, barMax = W - labW - 160;
  const colOf = (st) =>
    st === "done" ? "var(--green)" :
    st === "stopped" ? "var(--orange, #e0a23b)" :
    st === "error" ? "var(--red, #e0564f)" : "var(--accent)";
  let s = `<svg viewBox="0 0 ${W} ${H}">`;
  items.forEach((it, i) => {
    const y = padT + i * rowH;
    const cy = y + rowH / 2 - 6;
    const w = Math.max(3, (it.new_points / maxP) * barMax);
    const col = colOf(it.status);
    const dir = (it.direction || "?").length > 16 ? it.direction.slice(0, 16) + "…" : it.direction;
    s += `<text class="bar-label" x="6" y="${cy + 4}" style="font-weight:600">${esc(dir)}</text>`;
    s += `<rect x="${barX}" y="${y + 6}" width="${w.toFixed(1)}" height="22" rx="5" fill="${col}" opacity="0.85">` +
      `<title>新增 ${it.new_points} 条 · ${it.rounds} 轮 · 读 ${it.fetched} 篇 · 耗时 ${it.duration_s}s · 单轮 ${it.per_round} 条</title></rect>`;
    s += `<text class="axis-text" x="${(barX + w + 8).toFixed(1)}" y="${cy + 4}">${it.new_points} 条 · ${it.rounds} 轮 · 单轮 ${it.per_round}</text>`;
  });
  return s + `</svg>`;
}

async function loadAnalytics() {
  let d;
  try {
    d = await api("/api/analytics");
  } catch (e) {
    toast("分析数据获取失败：" + e.message, "err");
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
  setHTML($("#an-growth"), svgGrowth(d.growth));
  setHTML(
    $("#an-growth-range"),
    d.growth && d.growth.length
      ? `${d.growth[0].day} → ${d.growth[d.growth.length - 1].day}（${d.growth.length} 天）`
      : ""
  );
  // 学习活跃度热力图（复用 growth 数据）
  setHTML($("#an-heatmap"), svgHeatmap(d.growth));
  setHTML(
    $("#an-heat-range"),
    d.growth && d.growth.length ? `近 ${d.growth.length} 天有学习记录` : ""
  );
  setHTML($("#an-topics"), htmlTopicBars(d.topics));
  setHTML($("#an-conf"), svgHist(d.confidence));
  setHTML($("#an-source"), svgDonut(d.sources));
  setHTML($("#an-graph"), svgGraph(d.graph));
  // 学习历程：每次任务的产出条形
  setHTML($("#an-sessions"), svgSessions(d.sessions));
  setHTML(
    $("#an-sessions-sum"),
    d.sessions && d.sessions.total_sessions
      ? `${d.sessions.total_sessions} 次任务 · 共新增 ${d.sessions.total_points} 条 · 合计 ${d.sessions.total_rounds} 轮`
      : ""
  );
  // 检索性能卡片：命中率仪表 + 平均耗时迷你折线
  loadPerf();
}

let _perfHistory = [];
async function loadPerf() {
  let p;
  try {
    p = await api("/api/perf");
  } catch (_) {
    return;
  }
  const rate = Math.round((p.search_cache_hit_rate || 0) * 100);
  setHTML($("#an-perf-hit"), rate + "%");
  setHTML($("#an-perf-ms"), (p.search_avg_ms || 0).toFixed(2) + " ms");
  setHTML($("#an-perf-calls"), String(p.search_calls || 0));
  setHTML($("#an-perf-gauge"), svgGauge(rate));
  // 维护最近 20 次平均耗时的趋势
  _perfHistory.push(p.search_avg_ms || 0);
  if (_perfHistory.length > 20) _perfHistory.shift();
  setHTML($("#an-perf-spark"), svgSpark(_perfHistory));
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
  setHTML($("#an-search"), renderSearchExplain(d));
  setHTML($("#an-search-graph"), svgSearchGraph(d));
}

$("#btn-an-search")?.addEventListener("click", runSearchExplain);
$("#an-q")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") runSearchExplain();
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
    if (state.view === "dashboard") await loadDashboard(st);
    if (state.view === "knowledge") await loadKnowledge();
    if (state.view === "analytics") loadAnalytics();
  } catch (_) {}
  finally { refreshBusy = false; }
}

connectSSE();
switchView(location.hash.slice(1) || "dashboard", true);
setInterval(refreshAll, 6000);
