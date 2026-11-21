/* DOM 桩单测：近重复整理卡片（第十三轮新功能）+ 状态栏「检测中」。
   与 t_point.js 同一模式：从 web/app.js 抓函数、配桩全局后直接跑。 */
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
const els = {};
global.$ = (s) => (els[s] = els[s] || { innerHTML: "", dataset: {}, hidden: false, style: {} });
global.STAGE_TEXT = {};
global.updateRing = () => {};

let fails = 0;
function check(name, cond) {
  console.log((cond ? "  ✓ " : "  ✗ ") + name);
  if (!cond) fails++;
}

eval(grab("renderDuplicates"));
eval(grab("paintTopbar"));

// ---- 1. 有近重复对 ----
console.log("renderDuplicates 有重复对：");
renderDuplicates({
  threshold: 0.9, scanned: 120, capped: 0,
  pairs: [
    {
      sim: 0.9543,
      a: { id: "a1", topic: "光固化 · 收缩补偿", claim: "曝光能量与收缩率正相关，需按层高反向补偿", confidence: 0.72 },
      b: { id: "b1", topic: "光固化 · 收缩补偿", claim: "收缩率随曝光能量上升，补偿量按层高线性调整", confidence: 0.65 },
    },
    {
      sim: 0.9021,
      a: { id: "a2", topic: "几何节点", claim: "几何节点用场运算驱动实例分布", confidence: 0.8 },
      b: { id: "b2", topic: "几何节点", claim: "实例分布由场函数控制，可用于程序化生成", confidence: 0.6 },
    },
  ],
});
const html = els["#dup-body"].innerHTML;
check("卡片显示", els["#dup-card"].hidden === false);
check("说明扫描条数", /扫了 120 条/.test(html));
check("说明发现组数", /发现 2 组/.test(html));
check("两条都渲染", /曝光能量与收缩率正相关/.test(html) && /收缩率随曝光能量上升/.test(html));
check("相似度保留三位", /0\.954/.test(html) && /0\.902/.test(html));
check("相似度条按比例（0.954→77%）", /width:77%/.test(html));
check("每条都有『保留这条』", (html.match(/class="btn sm dup-keep"/g) || []).length === 4);
check("删除目标是对方 id", /data-drop="b1"/.test(html) && /data-drop="a1"/.test(html));
check("claim 可点开详情（data-pid）", /data-pid="a1"/.test(html) && /data-pid="b2"/.test(html));
check("带方向标签", /tagmini/.test(html));

// ---- 2. 库里够大但没有重复：给结论而不是空白 ----
console.log("renderDuplicates 无重复：");
renderDuplicates({ threshold: 0.9, scanned: 500, pairs: [] });
const none = els["#dup-body"].innerHTML;
check("卡片仍显示（给出结论）", els["#dup-card"].hidden === false);
check("说明扫了多少条", /扫了 500 条/.test(none));
check("明确说去重正常", /去重工作正常/.test(none));
check("提示可放宽阈值", /0\.85/.test(none));

// ---- 3. 库太小：整块隐藏，不占版面 ----
console.log("renderDuplicates 库太小：");
renderDuplicates({ threshold: 0.9, scanned: 6, pairs: [] });
check("卡片隐藏", els["#dup-card"].hidden === true);
check("内容被清空", els["#dup-body"].innerHTML === "");

// ---- 4. 转义 ----
console.log("renderDuplicates 转义：");
renderDuplicates({
  threshold: 0.9, scanned: 30,
  pairs: [{
    sim: 0.93,
    a: { id: 'x"1', topic: "<b>t</b>", claim: "<script>alert(1)</script>", confidence: 0.5 },
    b: { id: "y2", topic: "t", claim: "<img onerror=1>", confidence: 0.5 },
  }],
});
const xss = els["#dup-body"].innerHTML;
check("claim 尖括号被转义", !/<script>alert/.test(xss) && /&lt;script&gt;/.test(xss));
check("topic 标签失效", !/<b>t<\/b>/.test(xss));
check("id 引号被转义", /data-pid="x&quot;1"/.test(xss));
check("注入标签整体失效", !/<img onerror/.test(xss));

// ---- 5. 幂等 ----
console.log("renderDuplicates 幂等：");
const p = { threshold: 0.9, scanned: 50, pairs: [] };
renderDuplicates(p);
const w1 = writes;
renderDuplicates(p);
check("同数据二次渲染零写入", writes === w1);

// ---- 6. 状态栏：探测中不能误报「模型离线」----
console.log("paintTopbar 模型三态：");
const st = (llm) => ({
  stats: { points: 1, documents: 0, disk_gb: 1 },
  engine: { running: false, stage: "idle", round: 0 },
  llm, finetune: { cuda: false, ready: false },
});
paintTopbar(st({ healthy: true, models: [{ name: "m" }], checking: false }));
check("在线显示模型在线", els["#txt-llm"].textContent === "模型在线");
check("在线指示灯为 on", els["#dot-llm"].className === "dot on");
check("hover 给出模型数量", /1 个模型可用/.test(els["#dot-llm"].title));

paintTopbar(st({ healthy: false, models: [], checking: true }));
check("探测中显示检测中", els["#txt-llm"].textContent === "检测中…");
check("探测中指示灯为 warn（不是红）", els["#dot-llm"].className === "dot warn");

paintTopbar(st({ healthy: false, models: [], checking: false }));
check("确定离线才显示模型离线", els["#txt-llm"].textContent === "模型离线");
check("离线指示灯为 off", els["#dot-llm"].className === "dot off");
check("离线 hover 给出怎么修", /ollama serve/.test(els["#dot-llm"].title));

console.log(fails === 0 ? "\n全部通过" : `\n${fails} 项失败`);
process.exit(fails === 0 ? 0 : 1);
