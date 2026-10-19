// 批量 OCR 进度 + 抽屉实时预览 的浏览器端验收
import pw from "file:///C:/Users/xukunhang/.workbuddy-ai/binaries/node/workspace/node_modules/playwright-core/index.js";
const { chromium } = pw;

const BASE = "http://127.0.0.1:8765";
const BOOKS = ["155aa6460be2a1ea", "6be1462c495b80c8"];
const log = (...a) => console.log(...a);

const browser = await chromium.launch({
  executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
  headless: true,
  args: ["--disable-gpu"],
});
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errors = [];
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
page.on("console", (m) => {
  if (m.type() === "error") errors.push("console: " + m.text());
});
page.on("requestfailed", (r) => errors.push("reqfail: " + r.url().slice(0, 80)));
const bad = [];
page.on("response", (r) => { if (r.status() >= 400) bad.push(`http${r.status()}: ` + r.url().slice(0, 90)); });

await page.goto(BASE + "/#library", { waitUntil: "networkidle" });
await page.waitForTimeout(1500);

// 1) 发起批量 OCR（2 本 × 8 页，快速引擎）
const start = await page.evaluate(async (ids) => {
  const r = await fetch("/api/ocr/batch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ book_ids: ids, pages: "111-130", engine: "rapid" }),
  });
  return r.json();
}, BOOKS);
log("启动:", JSON.stringify(start));

// 2) 走修复后的按钮同款逻辑：立刻接上轮询
await page.evaluate(() => window.pollLibTask());

const t0 = Date.now();
await page.waitForFunction(
  () => {
    const w = document.querySelector("#lib-bar-wrap");
    return w && w.style.display === "block";
  },
  { timeout: 15000 }
);
log(`书库进度条出现耗时: ${Date.now() - t0}ms`);
log("书库进度文案:", (await page.textContent("#lib-task-hint")).trim());

// 3) 点「查看」→ 抽屉进入跟随模式
await page.click("#lib-ocr-peek");
await page.waitForTimeout(2500);

const snap = async (tag) => {
  const d = await page.evaluate(() => ({
    title: document.querySelector("#ocr-title").textContent.trim().slice(0, 34),
    status: document.querySelector("#ocr-status").textContent.trim(),
    bar: document.querySelector("#ocr-bar").style.width,
    img: (document.querySelector("#ocr-img").getAttribute("src") || "").slice(0, 52),
    pages: document.querySelectorAll("#ocr-page-list .pg").length,
    textLen: (document.querySelector("#ocr-text").value || "").length,
    startDisabled: document.querySelector("#ocr-start").disabled,
    stopDisabled: document.querySelector("#ocr-stop").disabled,
  }));
  log(`[${tag}]`, JSON.stringify(d));
  return d;
};

const s1 = await snap("抽屉已打开");
await page.screenshot({ path: "I:/workbuddyAI/2026-10-18-15-27-36/evomind/tests/shot-1-open.png" });

const titles = [s1.title];
let previewSeen = s1.img.includes("/page/");
for (let i = 0; i < 40; i++) {
  await page.waitForTimeout(1500);
  const s = await snap(`t+${(i + 1) * 1.5}s`);
  if (s.title && !titles.includes(s.title)) titles.push(s.title);
  if (s.img.includes("/page/")) previewSeen = true;
  const st = await page.evaluate(async () => (await fetch("/api/ocr/state")).json());
  if (!st.batch && !st.running) break;
}
await page.screenshot({ path: "I:/workbuddyAI/2026-10-18-15-27-36/evomind/tests/shot-2-run.png" });

// 4) 结束后应自动复位
await page.waitForTimeout(3500);
const after = await page.evaluate(() => ({
  barWrap: document.querySelector("#lib-bar-wrap").style.display,
  status: document.querySelector("#ocr-status").textContent.trim(),
  startDisabled: document.querySelector("#ocr-start").disabled,
  stopDisabled: document.querySelector("#ocr-stop").disabled,
}));
log("[结束后]", JSON.stringify(after));
await page.screenshot({ path: "I:/workbuddyAI/2026-10-18-15-27-36/evomind/tests/shot-3-done.png" });

log("=== 结论 ===");
log("抽屉跟随换书:", titles.length > 1, "标题序列:", titles);
log("出现页预览图:", previewSeen);
log("前端报错:", errors.length ? errors.slice(0, 5) : "无");
log("4xx 请求:", bad.length ? [...new Set(bad)] : "无");

await browser.close();
