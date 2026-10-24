// Run against an isolated service containing an already-refined book sample.
import { createRequire } from "node:module";
import assert from "node:assert/strict";
const { chromium } = createRequire(import.meta.url)("playwright-core");
const base = process.env.EVOMIND_TEST_URL || "http://127.0.0.1:8876";
const browser = await chromium.launch({ executablePath: process.env.EVOMIND_CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe", headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
try {
  await page.goto(base + "/#knowledge", { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => document.querySelectorAll("#kb-topic option").length > 1);
  assert.ok((await page.locator("#btn-refine-books").boundingBox()).y < 600, "one-click action must be near the top, above duplicate suggestions");
  await page.locator("#kb-topic").selectOption({ index: 1 });
  const completed = page.waitForResponse((r) => r.url().endsWith("/api/refinements/books/start"));
  await page.getByRole("button", { name: "一键炼化全部书籍", exact: true }).click();
  const result = await (await completed).json();
  assert.equal(result.ok, true);
  assert.equal(result.completed, true);
  assert.equal(result.pending, 0);
  await page.waitForFunction(() => document.querySelector("#refine-status").textContent.includes("全部书籍炼化完成"));
  assert.equal(await page.locator("#kb-topic").inputValue(), "");
  assert.equal(await page.locator("#refine-progress").evaluate((el) => el.value), 100);
  await page.locator(".refinement-result summary").first().click();
  assert.ok(await page.locator(".refinement-result [data-pid]").count() > 0);
  // Exercise pagination with deterministic history, without changing sample data.
  await page.route("**/api/refinements?*", (route) => {
    const offset = Number(new URL(route.request().url()).searchParams.get("offset") || 0);
    return route.fulfill({ json: { state: { running: false }, total: 45,
      items: Array.from({ length: Math.min(20, 45 - offset) }, (_, i) => ({
        topic: "Sample topic", book_title: `Sample Book ${offset + i + 1}`, input_count: 12,
        items: [{ claim: "Concise source-backed core", detail: "", source_ids: [] }],
        sources: [], before_chars: 1000, after_chars: 100, created_at: 100 + offset + i,
      })),
    }});
  });
  await page.locator('[data-view="learn"]').click();
  await page.locator('[data-view="knowledge"]').click();
  await page.waitForFunction(() => document.querySelector("#refine-page").textContent.includes("1/3页"));
  await page.locator("#btn-refine-next").click();
  await page.waitForFunction(() => document.querySelector("#refine-results").textContent.includes("Sample Book 21"));
  assert.match(await page.locator("#refine-page").textContent(), /2\/3页/);
  await page.locator("#btn-refine-prev").click();
  await page.waitForFunction(() => document.querySelector("#refine-page").textContent.includes("1/3页"));
  await page.unroute("**/api/refinements?*");
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.locator("#btn-refine-books").waitFor();
  await page.waitForFunction(() => Number(getComputedStyle(document.querySelector("#view-knowledge")).opacity) === 1);
  if (process.env.EVOMIND_SCREENSHOTS) await page.screenshot({ path: process.env.EVOMIND_SCREENSHOTS + "/one-click-books.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.locator("#btn-refine-books").isVisible(), true);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2), false);
  assert.deepEqual(errors, []);
  console.log("One-click books browser test passed: scope reset, full completion, progress, citations, pagination, mobile layout.");
} finally {
  await browser.close();
}
