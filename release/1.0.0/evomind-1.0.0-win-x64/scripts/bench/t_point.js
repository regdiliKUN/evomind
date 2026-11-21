/* DOM 桩单测：知识点详情抽屉渲染（第十二轮新功能）。
   与 t_health.js 同一模式：从 web/app.js 抓函数、配桩全局后直接跑。 */
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
global.fmtTime = (t) => `t:${t}`;
let writes = 0;
global.setHTML = (el, h) => {
  if (!el) return false;
  if (el.__last === h) return false;
  el.__last = h; el.innerHTML = h; writes++;
  return true;
};
const els = {};
global.$ = (s) => (els[s] = els[s] || { innerHTML: "", dataset: {} });

let fails = 0;
function check(name, cond) {
  console.log((cond ? "  ✓ " : "  ✗ ") + name);
  if (!cond) fails++;
}

eval(grab("renderPoint"));

// ---- 1. 有相似邻居 ----
console.log("renderPoint 有邻居：");
renderPoint({
  item: {
    id: "p1", topic: "光固化 3D 打印 · 收缩补偿",
    claim: "曝光能量与收缩率正相关，需按层高做反向补偿",
    detail: "第一行\n第二行",
    source_url: "https://example.com/a", source_title: "树脂收缩研究",
    tags: ["树脂", "补偿"], confidence: 0.72, created_at: 1700000000,
  },
  similar: [
    { id: "p2", topic: "光固化 3D 打印 · 收缩补偿", claim: "反向缩放是常见补偿手段", similarity: 0.9123 },
    { id: "p3", topic: "切片参数", claim: "层高越薄单层能量越低", similarity: 0.4512 },
  ],
});
const html = els["#pt-body"].innerHTML;
check("渲染 claim", /曝光能量与收缩率正相关/.test(html));
check("渲染 topic 标签", /class="pt-topic"/.test(html) && /收缩补偿/.test(html));
check("正文保留换行结构", /class="pt-detail"/.test(html));
check("可信度条按 72% 宽度", /width:72%/.test(html));
check("可信度数值两位小数", /0\.72/.test(html));
check("出处为可点链接", /href="https:\/\/example\.com\/a"/.test(html));
check("邻居数量写进标题", /语义最近的 2 条/.test(html));
check("相似条目带 data-pid", (html.match(/class="sim-item" data-pid=/g) || []).length === 2);
check("相似度条按比例", /width:91\.2%/.test(html) && /width:45\.1%/.test(html));
check("相似度数值保留三位", /相似 0\.912/.test(html));
check("有『只看这个方向』下钻", /data-goto="knowledge" data-topic="光固化 3D 打印 · 收缩补偿"/.test(html));
check("有『就这条提问』", /class="btn sm pt-ask"/.test(html));
check("有『复制链接』", /class="btn sm pt-copy"/.test(html));
check("提问带 claim 片段", /data-claim="曝光能量与收缩率正相关/.test(html));

// ---- 2. 无邻居：给引导而不是空白 ----
console.log("renderPoint 无邻居：");
renderPoint({
  item: { id: "p9", topic: "小众方向", claim: "一条孤立的结论", detail: "", confidence: 0.5 },
  similar: [],
});
const empty = els["#pt-body"].innerHTML;
check("空态有解释文案", /这条还比较孤立/.test(empty));
check("空态有去学习按钮", /data-goto="learn"/.test(empty));
check("空态标题写 0 条", /语义最近的 0 条/.test(empty));
check("无 detail 时不渲染正文块", !/class="pt-detail"/.test(empty));
check("无出处时给占位", /没有记录出处/.test(empty));

// ---- 3. 转义：claim / topic 里的引号尖括号不能破结构 ----
console.log("renderPoint 转义：");
renderPoint({
  item: { id: "pX", topic: '方向"引号"<b>', claim: "<script>x</script>", detail: "a<b>c", confidence: 0.4 },
  similar: [{ id: 'p"Y', topic: "<i>t</i>", claim: "<img onerror=1>", similarity: 0.3 }],
});
const xss = els["#pt-body"].innerHTML;
check("claim 尖括号被转义", !/<script>x<\/script>/.test(xss) && /&lt;script&gt;/.test(xss));
check("topic 引号被转义", /方向&quot;引号&quot;&lt;b&gt;/.test(xss));
check("detail 尖括号被转义", /a&lt;b&gt;c/.test(xss));
check("邻居 id 引号被转义", /data-pid="p&quot;Y"/.test(xss));
check("注入标签全部失效", !/<img onerror/.test(xss) && !/<i>t<\/i>/.test(xss));

// ---- 4. 重复渲染同一 payload 不重写（轮询时不闪）----
console.log("renderPoint 幂等：");
const payload = { item: { id: "p1", topic: "t", claim: "c", confidence: 0.5 }, similar: [] };
renderPoint(payload);
const w1 = writes;
renderPoint(payload);
check("同数据二次渲染零写入", writes === w1);

console.log(fails === 0 ? "\n全部通过" : `\n${fails} 项失败`);
process.exit(fails === 0 ? 0 : 1);
