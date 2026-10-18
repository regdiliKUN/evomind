// Runs the actual console script with a minimal DOM; no npm packages or server required.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function consoleApp(fetch) {
  const nodes = new Map();
  const element = () => ({
    value: "", dataset: {}, style: {}, innerHTML: "", textContent: "",
    addEventListener() {}, classList: { toggle() {} },
  });
  const document = {
    hidden: false,
    querySelector(selector) {
      if (!nodes.has(selector)) nodes.set(selector, element());
      return nodes.get(selector);
    },
    querySelectorAll: () => [],
    createElement: element,
  };
  const ctx = vm.createContext({
    document, fetch, URLSearchParams, console, Date,
    location: { hash: "#chat" }, window: { addEventListener() {} },
    EventSource: class { addEventListener() {} close() {} },
    setInterval() {}, setTimeout() {}, clearInterval() {}, clearTimeout() {},
  });
  const source = fs.readFileSync(path.join(__dirname, "../web/app.js"), "utf8");
  vm.runInContext(source + "\nglobalThis.app = {state, loadKnowledge, refreshAll};", ctx);
  return { app: ctx.app, document, nodes };
}

const response = (items = []) => ({ ok: true, json: async () => ({ items, total: items.length }) });

test("polling retains the submitted search and topic, and clearing resets the query", async () => {
  const urls = [];
  const { app, document } = consoleApp(async (url) => { urls.push(url); return response(); });
  document.querySelector("#kb-topic").value = "topic with spaces";
  await app.loadKnowledge("vector search");
  await app.loadKnowledge();
  for (const url of urls) {
    const params = new URL(url, "http://localhost").searchParams;
    assert.equal(params.get("q"), "vector search");
    assert.equal(params.get("topic"), "topic with spaces");
  }
  await app.loadKnowledge("");
  assert.equal(new URL(urls.at(-1), "http://localhost").searchParams.has("q"), false);
});

test("a late search response cannot replace the newer results", async () => {
  const pending = [];
  const { app, document } = consoleApp(() => new Promise((resolve) => pending.push(resolve)));
  const old = app.loadKnowledge("old");
  const newer = app.loadKnowledge("new");
  pending[1](response([{ claim: "new result", topic: "test", created_at: 1 }]));
  await newer;
  pending[0](response([{ claim: "stale result", topic: "test", created_at: 1 }]));
  await old;
  const html = document.querySelector("#kb-list").innerHTML;
  assert.match(html, /new result/);
  assert.doesNotMatch(html, /stale result/);
});

test("slow status requests do not overlap and hidden tabs skip polling", async () => {
  let requests = 0;
  let finish;
  const { app, document } = consoleApp(() => {
    requests++;
    return new Promise((resolve) => { finish = resolve; });
  });
  const first = app.refreshAll();
  await app.refreshAll();
  assert.equal(requests, 1);
  finish({ ok: true, json: async () => ({
    stats: { points: 0, documents: 0, disk_gb: 0 }, engine: { running: false },
    llm: { healthy: false }, finetune: { cuda: false, ready: false },
  }) });
  await first;
  document.hidden = true;
  await app.refreshAll();
  assert.equal(requests, 1);
});

test("polling recovers after an HTTP failure", async () => {
  let requests = 0;
  const { app } = consoleApp(async () => { requests++; throw new Error("offline"); });
  await app.refreshAll();
  await app.refreshAll();
  assert.equal(requests, 2);
});
