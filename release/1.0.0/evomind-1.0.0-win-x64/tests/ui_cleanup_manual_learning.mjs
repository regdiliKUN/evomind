// Isolated service only: fixture contains six book facts and one web duplicate.
import { createRequire } from "node:module";
import assert from "node:assert/strict";
const { chromium } = createRequire(import.meta.url)("playwright-core");
const base = process.env.EVOMIND_TEST_URL || "http://127.0.0.1:8876";
const browser = await chromium.launch({ executablePath: process.env.EVOMIND_CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe", headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
page.on("response", (r) => { if (r.url().includes("/api/") && r.status() === 404) errors.push("API 404: " + r.url()); });
try {
  await page.goto(base + "/#knowledge", { waitUntil: "domcontentloaded" });
  const before = await (await page.request.get(base + "/api/knowledge?limit=60")).json();
  const duplicate = before.items.find((p) => p.source_title === "Cleanup UI duplicate");
  assert.ok(duplicate, "seed the duplicate fixture first");
  const cleaned = page.waitForResponse((r) => r.url().endsWith("/api/knowledge/cleanup") && r.request().method() === "POST");
  await page.getByRole("button", { name: "一键清理重复项", exact: true }).click();
  assert.equal((await (await cleaned).json()).removed, 1);
  await page.waitForFunction(() => !document.querySelector("#btn-cleanup-undo").disabled);
  const archived = await (await page.request.get(base + "/api/knowledge/" + duplicate.id)).json();
  assert.equal(archived.item.archived, true);
  const restored = page.waitForResponse((r) => r.url().endsWith("/api/knowledge/cleanup/undo"));
  await page.getByRole("button", { name: "撤销上次清理", exact: true }).click();
  assert.equal((await (await restored).json()).restored, 1);
  const after = await (await page.request.get(base + "/api/knowledge?limit=60")).json();
  assert.equal(after.total, before.total);
  await page.locator('[data-view="learn"]').click();
  let startBody;
  await page.route("**/api/learn/start", (route) => {
    startBody = route.request().postDataJSON();
    return route.fulfill({ json: { ok: true } });
  });
  await page.getByRole("button", { name: "随机选一个方向", exact: true }).click();
  await page.waitForFunction(() => document.querySelector("#direction").value.startsWith("ZBrush："));
  assert.equal(startBody, undefined);
  assert.equal(await page.locator("#btn-start").isEnabled(), true);
  await page.locator("#rounds").selectOption("16");
  await page.getByRole("button", { name: "开始进化学习", exact: true }).click();
  await page.waitForFunction(() => !document.querySelector("#btn-stop").disabled);
  assert.equal(startBody.rounds, 16);
  assert.equal(startBody.direction, await page.locator("#direction").inputValue());
  assert.deepEqual(errors, []);
  console.log("Browser passed: cleanup/undo, archived sources, random selection without learning, manual sixteen-round start, no API 404.");
} finally { await browser.close(); }
