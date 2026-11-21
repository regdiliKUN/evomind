/* DOM 桩单测：svgGraph 交互结构 + mountGraph 空态引导。 */
const fs = require("fs");
const src = fs.readFileSync("web/app.js", "utf8");

function grab(name) {
  const m = src.match(new RegExp("(?:async )?function " + name + "\\([^)]*\\) \\{[\\s\\S]*?\\n\\}"));
  if (!m) { console.log(name, "NOT FOUND"); process.exit(1); }
  return m[0];
}

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const AN_PALETTE = ["#5b8def", "#3fae8a", "#c98b3a", "#8a6fd4", "#d96a6a"];
global.esc = esc;
global.AN_PALETTE = AN_PALETTE;
let htmlWrites = 0;
global.setHTML = (el, h) => { if (el.innerHTML === h) return false; el.innerHTML = h; htmlWrites++; return true; };
global.switchView = () => {};
global.api = async () => ({ items: [] });
global.toast = () => {};

// ---- 1. svgGraph 结构（可交互钩子存在）----
eval(grab("svgGraph"));
const graph = {
  nodes: [
    { topic: "ZBrush 硬表面雕刻", count: 42 },
    { topic: "Blender 几何节点", count: 18 },
    { topic: "光固化打印", count: 7 },
  ],
  // 后端 topic_graph 的 a/b 是 topic 字符串（analytics.topic_graph 的 topics[i]）
  edges: [
    { a: "ZBrush 硬表面雕刻", b: "Blender 几何节点", w: 0.66 },
    { a: "ZBrush 硬表面雕刻", b: "光固化打印", w: 0.38 },
  ],
};
const svg = svgGraph(graph);
console.log("svgGraph: <svg>:", svg.startsWith("<svg"), "| </svg>:", svg.endsWith("</svg>"));
console.log("  g.gnode 数:", (svg.match(/class="gnode"/g) || []).length, "(应为 3)");
console.log("  line.gedge 数:", (svg.match(/class="gedge"/g) || []).length, "(应为 2)");
console.log("  data-a/data-b:", /data-a="0" data-b="1"/.test(svg));
console.log("  title 含条数:", svg.includes("42 条知识点"));
console.log("  NaN/undefined:", /NaN|undefined/.test(svg));

// ---- 2. mountGraph 空态引导 ----
const els = {};
function fakeEl() {
  return { innerHTML: "", style: {}, dataset: {},
    addEventListener(ev, fn) { (this._h = this._h || {})[ev] = fn; },
    querySelector(s) { return this._qs && this._qs[s]; } };
}
global.$ = (s) => (els[s] = els[s] || fakeEl());
global._graphSeq = 0;
// 先装载 bindGraphInteractions（mountGraph 内部引用它），再装载 mountGraph 本体
const bindSrc = src.match(/function bindGraphInteractions\(box, graph, mySeq, detail\) \{[\s\S]*?\n\}\n/);
if (!bindSrc) { console.log("bindGraphInteractions NOT FOUND"); process.exit(1); }
eval(bindSrc[0]);
const mountSrc = src.match(/async function mountGraph\(graph\) \{[\s\S]*?\n\}\n/);
if (!mountSrc) { console.log("mountGraph NOT FOUND"); process.exit(1); }
eval(mountSrc[0]);

(async () => {
  await mountGraph(null);
  const box = els["#an-graph"];
  console.log("空态引导: 含「至少两个学习方向」:", box.innerHTML.includes("至少两个学习方向"));
  console.log("  含去定方向按钮:", box.innerHTML.includes('data-goto="learn"'));
  console.log("  detail 已清空:", els["#an-graph-detail"].innerHTML === "");

  await mountGraph({ nodes: [], edges: [] });
  console.log("空 nodes 同样走引导:", els["#an-graph"].innerHTML.includes("去定方向"));

  await mountGraph(graph);
  console.log("有数据: 渲染 SVG:", els["#an-graph"].innerHTML.startsWith("<svg"));
  console.log("  detail 提示语:", els["#an-graph-detail"].innerHTML.includes("悬停"));

  console.log("全部完成");
})();
