/* DOM 桩单测：知识库健康度卡片渲染 + 方向分布条下钻标记 + 版本标签。
   与 t_graph.js 同一模式：从 web/app.js 源码抓函数，配桩全局后直接跑。 */
const fs = require("fs");
const src = fs.readFileSync("web/app.js", "utf8");

function grab(name) {
  const m = src.match(new RegExp("(?:async )?function " + name + "\\([^)]*\\) \\{[\\s\\S]*?\\n\\}"));
  if (!m) { console.log(name, "NOT FOUND"); process.exit(1); }
  return m[0];
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
global.esc = esc;
let writes = 0;
global.setHTML = (el, h) => {
  if (!el) return false;
  if (el.__last === h) return false;
  el.__last = h; el.innerHTML = h; writes++;
  return true;
};
global.svgGauge = (rate, label) => `<svg data-gauge="${rate|0}" data-label="${label}"></svg>`;
const els = {};
global.$ = (s) => (els[s] = els[s] || { innerHTML: "", dataset: {} });

let fails = 0;
function check(name, cond) {
  console.log((cond ? "  ✓ " : "  ✗ ") + name);
  if (!cond) fails++;
}

// ---- 1. 空库：引导而不是「暂无数据」----
console.log("renderHealth 空库：");
eval(grab("renderHealth"));
renderHealth({ empty: true, score: 0, level: "空库", dims: [], weakest: null });
const emptyHtml = els["#health-dims"].innerHTML;
check("有空态引导文案", /还是空的/.test(emptyHtml));
check("有去定方向按钮", /data-goto="learn"/.test(els["#health-weak"].innerHTML));
check("仪表盘清空", els["#health-gauge"].innerHTML === "");
check("等级显示空库", els["#health-level"].innerHTML === "空库");

// ---- 2. 有数据：仪表 + 四维度 + 最薄弱引导 ----
console.log("renderHealth 有数据：");
const h = {
  ver: 12, empty: false, score: 64, level: "起步",
  dims: [
    { key: "scale", label: "知识规模", score: 40, detail: "共 48 条 · 3 个方向", hint: "规模还不够，多跑几轮或补新方向", view: "learn", btn: "去学习" },
    { key: "confidence", label: "可信度", score: 90, detail: "平均 0.75", hint: "可信度健康", view: "chat", btn: "去提问" },
    { key: "sources", label: "信源多样性", score: 30, detail: "1 类信源 · web 占 100%", hint: "信源太单一，混着学不容易有盲区", view: "resources", btn: "找找资料" },
    { key: "freshness", label: "学习新鲜度", score: 96, detail: "最后学习 0 天前 · 近 7 天 +12 条", hint: "学习节奏正常", view: "learn", btn: "继续学" },
  ],
  weakest: { key: "sources", label: "信源多样性", score: 30, hint: "信源太单一，混着学不容易有盲区", view: "resources", btn: "找找资料" },
};
renderHealth(h);
check("仪表盘渲染 64 分", /data-gauge="64"/.test(els["#health-gauge"].innerHTML));
check("等级标签", els["#health-level"].innerHTML === "起步");
check("四个维度条", (els["#health-dims"].innerHTML.match(/class="health-dim"/g) || []).length === 4);
check("低分维度红色条", /var\(--orange/.test(els["#health-dims"].innerHTML));
check("维度详情文案", /共 48 条/.test(els["#health-dims"].innerHTML));
const weakHtml = els["#health-weak"].innerHTML;
check("最薄弱项点名", /信源多样性/.test(weakHtml) && /30 分/.test(weakHtml));
check("最薄弱项引导按钮跳资源页", /data-goto="resources"/.test(weakHtml));

// ---- 3. 内容没变不重建（轮询时卡片不闪）----
console.log("renderHealth 幂等：");
const before = writes;
renderHealth(h);
check("同数据二次渲染零写入", writes === before);

// ---- 4. 方向分布条下钻标记 ----
console.log("htmlTopicBars 下钻：");
eval(grab("htmlTopicBars"));
const bars = htmlTopicBars([
  { topic: "方向甲 · 光固化", count: 42, avg_conf: 0.71 },
  { topic: '带"引号"的方向', count: 8, avg_conf: 0.5 },
]);
check("每行带 kb-drill 类", (bars.match(/class="kb-drill"/g) || []).length === 2);
check("带 data-topic 属性", /data-topic="方向甲 · 光固化"/.test(bars));
check("topic 引号被转义", /data-topic="带&quot;引号&quot;的方向"/.test(bars));
check("有条形与条数", /class="bar"/.test(bars) && /42 · 可信 0.71/.test(bars));
check("空列表有空态", /还没有任何学习记录/.test(htmlTopicBars([])));

// ---- 5. 版本标签 ----
console.log("paintAnVer：");
eval(grab("paintAnVer"));
const verEl = { innerHTML: "", dataset: {} };
els["#an-ver"] = verEl;
paintAnVer({ ver: 7 });
check("首次显示版本号", /数据版本 #7/.test(verEl.innerHTML));
const w1 = writes;
paintAnVer({ ver: 7 });
check("版本没变不重写", writes === w1);
paintAnVer({ ver: 8 });
check("版本变化才更新", /数据版本 #8/.test(verEl.innerHTML));

// ---- 6. 热力图自适应显示尺寸（修窄 viewBox 被拉满卡片的渲染 bug）----
console.log("svgHeatmap 尺寸：");
eval(grab("svgHeatmap"));
const hm = svgHeatmap([{ day: "2026-10-13", added: 2 }, { day: "2026-10-20", added: 9 }]);
const vb = hm.match(/viewBox="0 0 (\d+) (\d+)"/);
const wh = hm.match(/width="(\d+)" height="(\d+)"/);
check("带 viewBox", !!vb);
check("带显式宽高", !!wh);
if (vb && wh) {
  const W = +vb[1], H = +vb[2], w = +wh[1], h = +wh[2];
  check("宽高按同一比例缩放", Math.abs(w / W - h / H) < 0.01);
  check("少数据时放大到 1~3 倍", w >= W && w <= W * 3 + 1);
}
const hmLong = svgHeatmap(
  Array.from({ length: 40 }, (_, i) => ({ day: `2026-09-${String((i % 30) + 1).padStart(2, "0")}`, added: i % 5 }))
);
check("空输入有空态", /还没有学习记录/.test(svgHeatmap([])));

console.log(fails === 0 ? "\n全部通过" : `\n${fails} 项失败`);
process.exit(fails === 0 ? 0 : 1);
