const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
function element() {
  return { disabled: true, hidden: true, listeners: {},
    addEventListener(name, fn) { this.listeners[name] = fn; },
    removeEventListener(name) { delete this.listeners[name]; }, setAttribute() {} };
}
const undo = element(), redo = element(), message = element(), toolbar = element();
const root = { querySelector(selector) {
  return selector.includes('"undo"') ? undo : selector.includes('"redo"') ? redo : selector.includes("message") ? message : toolbar;
} };
const context = { window: {}, document: {}, Error, JSON, encodeURIComponent };
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../src/network_copilot/static/js/history-toolbar.js"), "utf8"), context);
(async () => {
  let applied, fail = false, requests = [];
  const panel = context.window.NetAIHistory.mount(root, {
    request: async (url) => {
      requests.push(url);
      if (fail) throw Error("offline");
      return { can_undo: !url.includes("/undo"), can_redo: url.includes("/undo"), snapshot: { n: 1 } };
    },
    onSnapshot: (snapshot) => { applied = snapshot; }, topologyId: "lab/a",
  });
  assert.equal(undo.disabled, true);
  undo.listeners.click();
  redo.listeners.click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.length, 0, "Disabled actions must not submit requests");
  await panel.refresh();
  assert.equal(undo.disabled, false); assert.equal(redo.disabled, true);
  assert.equal(applied.n, 1); assert.ok(requests[0].includes("lab%2Fa"));
  await undo.listeners.click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(undo.disabled, true); assert.equal(redo.disabled, false);
  fail = true;
  await assert.rejects(panel.refresh(), /offline/);
  assert.equal(undo.disabled, true); assert.equal(redo.disabled, true);
  assert.equal(message.hidden, false);
  assert.equal(message.textContent, "offline");
  fail = false; await panel.refresh();
  panel.destroy();
  assert.equal(undo.disabled, true); assert.equal(Object.keys(undo.listeners).length, 0);
  await assert.rejects(panel.refresh(), /destroyed/);
  const count = requests.length;
  await assert.rejects(panel.exportBackup(), /destroyed/);
  assert.equal(requests.length, count, "Destroyed controller must not export");
  console.log("History toolbar: passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
