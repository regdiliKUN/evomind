/* 引导 + 下一步建议的 DOM 桩单测（不依赖浏览器）。 */
const fs = require("fs");
const src = fs.readFileSync("web/app.js", "utf8");
function grab(name) {
  const m = src.match(new RegExp("(?:async )?function " + name + "\\([^)]*\\) \\{[\\s\\S]*?\\n\\}"));
  if (!m) { console.log(name, "NOT FOUND"); process.exit(1); }
  return m[0];
}
const els = {};
function fakeEl() {
  return { innerHTML: "", textContent: "", disabled: false, hidden: true, style: {},
           addEventListener() {}, dataset: {}, classList: { toggle() {} } };
}
global.$ = (s) => (els[s] = els[s] || fakeEl());
global.$$ = () => [];
global.esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
global.setHTML = (el, h) => { if (el.innerHTML === h) return false; el.innerHTML = h; return true; };
global.state = { view: "dashboard", status: null, suggestions: null };
global.switchView = () => {};
global.localStorage = { getItem: () => null, setItem: () => {} };
global.OB_STEPS = null;

// renderOnboarding / openOnboarding / closeOnboarding 依赖模块级 OB_STEPS / obStep，
// 直接从源码里连同常量一起取出执行（把 let obStep 提成全局，便于测试驱动）
const obBlock = src.match(/const OB_STEPS = \[[\s\S]*?\];\nlet obStep = 0;\nconst OB_KEY = "evomind\.onboarded\.v1";\n\nfunction renderOnboarding\(\) \{[\s\S]*?\n\}\n\nfunction openOnboarding\(\) \{[\s\S]*?\n\}\n\nfunction closeOnboarding\(\) \{[\s\S]*?\n\}/);
if (!obBlock) { console.log("OB block NOT FOUND"); process.exit(1); }
global.obStep = 0;
eval(obBlock[0].replace("let obStep = 0;", ""));
eval(grab("localNextSteps"));

// 1) 引导渲染
renderOnboarding();
console.log("第1步:", els["#ob-step"].innerHTML.includes("第一步"),
  "| dots:", (els["#ob-dots"].innerHTML.match(/<i/g) || []).length,
  "| next 按钮:", els["#ob-next"].textContent);
obStep = 1; renderOnboarding();
console.log("第2步 next 仍为下一步:", els["#ob-next"].textContent === "下一步");
obStep = 2; renderOnboarding();
console.log("第3步 next 变为开始使用:", els["#ob-next"].textContent === "开始使用");
console.log("  动作按钮:", els["#ob-step"].innerHTML.includes("data-ob-action=\"analytics\""));
// 2) open/close
openOnboarding();
console.log("open 后 visible:", els["#onboarding"].hidden === false, "| 回到第1步:", els["#ob-step"].innerHTML.includes("第一步"));
closeOnboarding();
console.log("close 后 hidden:", els["#onboarding"].hidden === true);

// 3) localNextSteps 兜底（空库 / 中等库 / 大库 + 微调未就绪）
const mkSt = (points, ready) => ({
  stats: { points, scale_warn: "", resources: 3, topics: [] },
  finetune: { ready },
  config: { auto_download: true },
});
const empty = localNextSteps(mkSt(0, true));
console.log("空库兜底首条:", empty[0].title, "| 跳转:", empty[0].view);
const mid = localNextSteps(mkSt(50, false));
console.log("50 条兜底首条:", mid[0].title, "| 含微调提示:", mid.some((i) => i.title.includes("微调环境")));
const big = localNextSteps(mkSt(200, true));
console.log("200 条兜底首条:", big[0].title);

// 4) renderNextSteps：后端建议优先
eval(grab("renderNextSteps"));
const st = mkSt(50, false);
els["#next-steps"] = fakeEl();
renderNextSteps(st);
console.log("无后端建议时用兜底:", els["#next-steps"].innerHTML.includes("检验一下"));
state.suggestions = [
  { level: "warn", icon: "◔", title: "已经 9 天没有新增", detail: "学习停滞比学得慢更值得关注。", view: "learn", btn: "继续学" },
  { level: "act", icon: "◕", title: "检验一下学得怎么样", detail: "去「检验所学」问几个真问题。", view: "chat", btn: "去提问" },
];
renderNextSteps(st);
console.log("有后端建议时用之:", els["#next-steps"].innerHTML.includes("已经 9 天没有新增"),
  "| 级别色点:", els["#next-steps"].innerHTML.includes("ns-level warn"));
// 内容不变不重建
const before = els["#next-steps"].innerHTML;
renderNextSteps(st);
console.log("内容不变则不重建:", before === els["#next-steps"].innerHTML);
