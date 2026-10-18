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
  const params = new URLSearchParams({ limit: "300" });
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
      const canPick = ["pending", "ready"].includes(b.status);
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
  btn.textContent = `导入选中（${n}）`;
  btn.disabled = n === 0;
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
    const t = await api("/api/library/state");
    paintLibTask(t);
    if (!t.running) {
      loadLibrary();
    }
  } catch (_) {}
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

$("#btn-lib-selectall").addEventListener("click", () => {
  $$("#lib-book-list .lib-ck:not(:disabled)").forEach((ck) => {
    ck.checked = true;
    libSelected.add(ck.dataset.id);
  });
  updateLibImportBtn();
});

/* ---------------- OCR 识别与校对 ---------------- */
let ocrBook = null;
let ocrPageNo = 0;
let ocrPoll = null;

function openOcr(book) {
  ocrBook = book;
  ocrPageNo = 0;
  $("#ocr-title").textContent = book.title || book.path;
  $("#ocr-sub").textContent = `${(book.format || "").toUpperCase()} · ${book.pages || "?"} 页`;
  $("#ocr-text").value = "";
  $("#ocr-page-info").textContent = "";
  $("#ocr-img").removeAttribute("src");
  $("#ocr-drawer").classList.add("open");
  loadOcrPages();
  pollOcr();
}

function closeOcr() {
  $("#ocr-drawer").classList.remove("open");
  ocrBook = null;
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

async function selectOcrPage(pno) {
  if (!ocrBook) return;
  ocrPageNo = pno;
  $$("#ocr-page-list .pg").forEach((el) =>
    el.classList.toggle("active", +el.dataset.pg === pno)
  );
  $("#ocr-img").src = `/api/library/books/${ocrBook.id}/page/${pno}.png?dpi=120`;
  $("#ocr-page-info").textContent = "加载中…";
  try {
    const d = await api(`/api/library/books/${ocrBook.id}/ocr/${pno}`);
    $("#ocr-text").value = d.text || "";
    $("#ocr-page-info").textContent =
      `${d.chars} 字 · ${d.engine}${d.elapsed ? " · " + d.elapsed + "s" : ""}${d.edited ? " · 已校对" : ""}`;
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
      engine === "vision" ? "精细识别已开始，每页约 40 秒" : "识别已开始，每页约 5 秒",
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
    paintOcrTask(s);
    if (s.running) {
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
  const mine = s.running && ocrBook && s.book_id === ocrBook.id;
  $("#ocr-start").disabled = !!s.running;
  $("#ocr-stop").disabled = !s.running;
  if (s.running) {
    const pct = s.total ? Math.round((s.done / s.total) * 100) : 0;
    $("#ocr-bar").style.width = pct + "%";
    $("#ocr-status").textContent = `${s.done}/${s.total} 页 · 第 ${s.page} 页 · ${s.chars} 字 · ${s.elapsed}s`;
  } else {
    $("#ocr-bar").style.width = "0%";
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

$("#btn-ft-dataset").addEventListener("click", async () => {
  toast("正在生成训练集…");
  try {
    const r = await api("/api/finetune/dataset", { method: "POST", body: {} });
    if (r.ok) {
      state.datasetPath = r.path;
      $("#ft-ds").textContent = `${r.count} 条`;
      $("#btn-ft-train").disabled = false;
      toast(`训练集已生成：${r.count} 条`, "ok");
    } else {
      toast(r.msg || "生成失败", "err");
    }
  } catch (e) {
    toast("生成失败：" + e.message, "err");
  }
});

$("#btn-ft-train").addEventListener("click", async () => {
  if (!state.datasetPath) return toast("先生成训练集", "err");
  try {
    const r = await api("/api/finetune/train", {
      method: "POST",
      body: { dataset_path: state.datasetPath },
    });
    toast(r.msg || "已启动", "ok");
    $("#ft-train").textContent = "训练中…";
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
  } catch (_) {}
  finally { refreshBusy = false; }
}

connectSSE();
switchView(location.hash.slice(1) || "dashboard", true);
setInterval(refreshAll, 6000);
