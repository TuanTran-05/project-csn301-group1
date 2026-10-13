const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const settle = () => new Promise(resolve => setImmediate(resolve));
function element() {
  return { disabled: true, hidden: true, listeners: {},
    addEventListener(name, fn) { this.listeners[name] = fn; },
    removeEventListener(name) { delete this.listeners[name]; }, setAttribute() {} };
}
const undo = element(), redo = element(), message = element(), toolbar = element();
const root = { querySelector(s) {
  return s.includes('"undo"') ? undo : s.includes('"redo"') ? redo : s.includes("message") ? message : toolbar;
} };
let factory, init;
const timers = new Map();
let timerId = 0;
const context = { window: {}, Error, JSON, encodeURIComponent,
  document: { addEventListener(name, callback) { init = callback; } },
  Alpine: { data(name, callback) { assert.equal(name, "historyDraft"); factory = callback; } },
  setInterval(callback) { timers.set(++timerId, callback); return timerId; },
  clearInterval(id) { timers.delete(id); },
};
for (const filename of ["history-toolbar.js", "history-panel.js"]) {
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../src/network_copilot/static/js", filename), "utf8"), context);
  context.NetAIHistory = context.window.NetAIHistory;
}
init();
(async () => {
  const watchers = {}, calls = [], events = [];
  let pendingResolve;
  const panel = Object.assign(factory(), {
    historyApiBase: "/api/draft-history",
    token: null, currentUser: null, $el: root,
    changesById: { 8: { status: "pending_approval" } }, batchesById: { 9: { status: "approved" } },
    $watch(name, callback) { watchers[name] = callback; },
    $dispatch(name, detail) { events.push([name, detail]); },
    async authFetch(url) {
      calls.push(url);
      if (this.token === "old-session") return new Promise(resolve => { pendingResolve = resolve; });
      return { can_undo: true, can_redo: false, snapshot: { interfaces: { "Gi0/0": { description: "from-api" } } } };
    },
  });
  panel.init();
  assert.equal(calls.length, 0); assert.equal(timers.size, 0);
  panel.token = "old-session"; panel.currentUser = { role: "ADMIN" };
  watchers.token();
  assert.equal(undo.disabled, true);
  const previousPanel = panel.historyPanel;
  panel.token = "new-session";
  const countBeforeSwitch = calls.length;
  await assert.rejects(previousPanel.exportBackup(), /Phiên đăng nhập/);
  assert.equal(calls.length, countBeforeSwitch, "Changed session must be rejected before sending");
  watchers.token();
  await settle();
  assert.equal(undo.disabled, false); assert.equal(redo.disabled, true);
  pendingResolve({ can_undo: false, can_redo: true, snapshot: { stale: true } });
  await settle();
  assert.equal(undo.disabled, false); assert.equal(redo.disabled, true);
  assert.equal(panel.draftSnapshot.interfaces["Gi0/0"].description, "from-api");
  assert.equal(panel.changesById[8].status, "pending_approval");
  assert.equal(panel.batchesById[9].status, "approved");
  assert.ok(calls.every(url => url.startsWith("/api/draft-history?")));
  assert.equal(timers.size, 1);
  const countBeforePendingChange = calls.length;
  watchers.pendingHistoryRevision();
  watchers.pendingHistoryRevision();
  await settle();
  assert.equal(calls.length, countBeforePendingChange + 1, "Pending changes coalesce into one status refresh");
  panel.token = null; panel.currentUser = null; watchers.token();
  assert.equal(timers.size, 0); assert.equal(panel.draftSnapshot, null);
  assert.equal(undo.disabled, true); assert.equal(redo.disabled, true);
  panel.token = "viewer"; panel.currentUser = { role: "VIEWER" }; watchers.currentUser();
  assert.equal(timers.size, 0); assert.equal(panel.historyPanel, null);
  panel.destroy();
  console.log("History panel lifecycle: passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
