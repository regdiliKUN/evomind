// 书库页（抽屉关闭时）批量 OCR 进度条的样子
import pw from "file:///C:/Users/xukunhang/.workbuddy-ai/binaries/node/workspace/node_modules/playwright-core/index.js";
const { chromium } = pw;

const BASE = "http://127.0.0.1:8765";
const BOOKS = ["155aa6460be2a1ea", "6be1462c495b80c8"];

const browser = await chromium.launch({
  executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
  headless: true,
});
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
await page.goto(BASE + "/#library", { waitUntil: "networkidle" });
await page.waitForTimeout(1200);

await page.evaluate(async (ids) => {
  await fetch("/api/ocr/batch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ book_ids: ids, pages: "131-160", engine: "rapid" }),
  });
  window.pollLibTask();
}, BOOKS);

await page.waitForTimeout(9000);
console.log("hint:", (await page.textContent("#lib-task-hint")).trim());
await page.screenshot({ path: "I:/workbuddyAI/2026-10-18-15-27-36/evomind/tests/shot-lib.png" });

// 中途刷新页面，验证进度能自动接回来
await page.reload({ waitUntil: "networkidle" });
await page.waitForTimeout(3000);
const after = await page.evaluate(() => ({
  bar: document.querySelector("#lib-bar-wrap").style.display,
  hint: document.querySelector("#lib-task-hint").textContent.trim(),
}));
console.log("刷新后:", JSON.stringify(after));
await page.screenshot({ path: "I:/workbuddyAI/2026-10-18-15-27-36/evomind/tests/shot-lib-reload.png" });

await page.evaluate(() => fetch("/api/ocr/stop", { method: "POST" }));
await browser.close();
