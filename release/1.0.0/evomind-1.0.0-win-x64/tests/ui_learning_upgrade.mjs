// Browser smoke test against an isolated EvoMind instance. Requires playwright-core.
import { createRequire } from "node:module";
import assert from "node:assert/strict";
const { chromium } = createRequire(import.meta.url)("playwright-core");
const base = process.env.EVOMIND_TEST_URL || "http://127.0.0.1:8876";
const browser = await chromium.launch({
  executablePath: process.env.EVOMIND_CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe",
  headless: true,
});
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
try {
  await page.goto(base + "/#learn", { waitUntil: "domcontentloaded" });
  await page.locator("#direction-seed").fill("ZBrush");
  await page.locator("#btn-directions").click();
  await page.waitForFunction(() => document.querySelectorAll(".direction-choice").length === 6);
  const first = await page.locator(".direction-choice").allTextContents();
  await page.locator(".direction-choice").first().click();
  assert.equal(await page.locator("#direction").inputValue(), first[0]);
  await page.locator("#btn-directions").click();
  await page.waitForFunction((previous) => Array.from(document.querySelectorAll(".direction-choice")).every((el) => !previous.includes(el.textContent)), first);
  await page.locator("#auto-seconds").fill("30");
  await page.locator("#auto-rounds").fill("3");
  const saved = page.waitForResponse((r) => r.url().endsWith("/api/config") && r.request().method() === "POST");
  await page.locator("#btn-auto-save").click();
  assert.equal((await (await saved).json()).idle_seconds, 30);
  // Route only the learning admission; no uncontrolled background model work.
  let selected;
  let submittedRounds;
  await page.route("**/api/learn/start", (route) => {
    selected = route.request().postDataJSON().direction;
    submittedRounds = route.request().postDataJSON().rounds;
    return route.fulfill({ json: { ok: true, session_id: "ui-test" } });
  });
  await page.locator("#btn-random-learn").click();
  assert.equal(selected, undefined, "random choice must not start learning");
  await page.locator("#rounds").selectOption("16");
  await page.locator("#btn-start").click();
  await page.waitForFunction(() => document.querySelector("#btn-stop").disabled === false);
  assert.ok(selected?.startsWith("ZBrush："));
  assert.equal(submittedRounds, 16);
  assert.equal(await page.locator("#direction").inputValue(), selected);
  if (process.env.EVOMIND_SCREENSHOTS) await page.screenshot({ path: process.env.EVOMIND_SCREENSHOTS + "/learn.png", fullPage: true });
  await page.locator('[data-view="knowledge"]').click();
  await page.locator(".refinement-result").first().waitFor();
  await page.locator(".refinement-result summary").first().click();
  await page.locator(".refinement-result [data-pid]").first().click();
  await page.waitForFunction(() => document.querySelector("#pt-body")?.textContent?.length > 20);
  await page.keyboard.press("Escape");
  if (process.env.EVOMIND_SCREENSHOTS) await page.screenshot({ path: process.env.EVOMIND_SCREENSHOTS + "/knowledge.png", fullPage: true });
  for (const view of ["library", "analytics", "resources", "finetune", "settings", "dashboard"]) {
    await page.locator(`[data-view="${view}"]`).click();
    assert.equal(await page.locator("#view-" + view).evaluate((el) => el.classList.contains("active")), true);
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(base + "/#learn", { waitUntil: "domcontentloaded" });
  assert.equal(await page.locator("#btn-directions").isVisible(), true);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2);
  assert.equal(overflow, false, "mobile layout overflows horizontally");
  assert.deepEqual(errors, []);
  console.log("Browser smoke passed: fresh directions, selection, random start, settings, source drawer, all views, mobile layout.");
} finally {
  await browser.close();
}
