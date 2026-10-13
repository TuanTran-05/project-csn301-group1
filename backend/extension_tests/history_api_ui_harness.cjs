/* UI controller -> JSON pipe -> actual Flask test client. No network sockets. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const readline = require("node:readline");
const lines = readline.createInterface({ input: process.stdin });
const pending = new Map();
let sequence = 0, completed = 0, register;
const watchdog = setTimeout(() => { console.error("Flask UI bridge timed out"); process.exit(3); }, 15000);
lines.on("line", line => {
  const response = JSON.parse(line), handlers = pending.get(response.id);
  if (!handlers) return;
  pending.delete(response.id); completed++;
  if (response.status >= 400) {
    const error = new Error(response.data.message || "API error");
    error.status = response.status; handlers.reject(error);
  } else handlers.resolve(response.data);
});
function request(url, options = {}) {
  return new Promise((resolve, reject) => {
    pending.set(++sequence, { resolve, reject });
    process.stdout.write(JSON.stringify({ id: sequence, url, options }) + "\n");
  });
}
function element() {
  return { disabled: true, hidden: true, listeners: {},
    addEventListener(name, fn) { this.listeners[name] = fn; },
    removeEventListener(name) { delete this.listeners[name]; }, setAttribute() {} };
}
const undo = element(), redo = element(), message = element(), toolbar = element();
const root = { querySelector(s) {
  return s.includes('"undo"') ? undo : s.includes('"redo"') ? redo : s.includes("message") ? message : toolbar;
} };
const timers = new Set();
const context = { window: {}, Error, JSON, encodeURIComponent,
  document: { addEventListener(name, fn) { register = fn; } },
  Alpine: { data(name, factory) { context.factory = factory; } },
  setInterval(fn) { timers.add(fn); return fn; }, clearInterval(fn) { timers.delete(fn); },
};
for (const filename of ["history-toolbar.js", "history-panel.js"]) {
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../src/network_copilot/static/js", filename), "utf8"), context);
  context.NetAIHistory = context.window.NetAIHistory;
}
register();
const normalize = value => JSON.parse(JSON.stringify(value));
(async () => {
  const events = [], pendingChanges = [{ id: 81, status: "pending_approval" }];
  const pendingBatches = [{ id: 82, status: "approved" }];
  const panel = Object.assign(context.factory(), {
    historyApiBase: "/api/draft-history",
    token: "provided-by-test-transport", currentUser: { role: "ADMIN" }, $el: root,
    pendingChanges, pendingBatches,
    changesById: { 81: pendingChanges[0] }, batchesById: { 82: pendingBatches[0] },
    $watch() {}, $dispatch(name, detail) { events.push([name, normalize(detail)]); },
    authFetch: request,
  });
  const originalPending = normalize([panel.pendingChanges, panel.pendingBatches, panel.changesById, panel.batchesById]);
  panel.init();
  while (completed < 1) await new Promise(resolve => setTimeout(resolve, 5));
  assert.equal(panel.draftSnapshot, null);
  assert.equal(undo.disabled, true); assert.equal(redo.disabled, true);
  undo.listeners.click(); redo.listeners.click();
  assert.equal(sequence, 1);

  // Explicit draft test input, never inventory or a device operation.
  const before = { interfaces: { "Gi0/0": { description: "before", enabled: true } } };
  const after = { interfaces: { "Gi0/0": { description: "after", enabled: false } } };
  await panel.historyPanel.push(before, "BASELINE", "Initial draft supplied by test");
  assert.equal(undo.disabled, true); assert.equal(redo.disabled, true);
  await panel.historyPanel.push(after, "CONFIG_INT", "Edit supplied draft");
  assert.deepEqual(normalize(panel.draftSnapshot), after);
  assert.equal(undo.disabled, false); assert.equal(redo.disabled, true);

  const undoPromise = undo.listeners.click();
  assert.equal(undo.disabled, true); assert.equal(redo.disabled, true);
  const count = sequence;
  undo.listeners.click(); redo.listeners.click();
  assert.equal(sequence, count, "Busy actions cannot send twice");
  await undoPromise;
  assert.deepEqual(normalize(panel.draftSnapshot), before);
  assert.equal(undo.disabled, true); assert.equal(redo.disabled, false);
  const backup = await panel.historyPanel.exportBackup();
  assert.deepEqual(backup.audit_trail.map(e => e.action_type), ["BASELINE", "CONFIG_INT", "UNDO"]);
  assert.ok(backup.audit_trail.every(e => e.actor === "actor-1"));
  assert.equal(backup.audit_trail[2].snapshot.kind, "json-object");
  assert.equal(backup.backup_version, "1.2");
  assert.equal("topology_id" in backup, false);

  await redo.listeners.click();
  assert.deepEqual(normalize(panel.draftSnapshot), after);
  assert.equal(redo.disabled, true);
  await panel.historyPanel.restore(backup);
  assert.deepEqual(normalize(panel.draftSnapshot), before);
  assert.equal(redo.disabled, false);
  await redo.listeners.click();
  assert.deepEqual(normalize(panel.draftSnapshot), after);
  await undo.listeners.click();
  const branched = { interfaces: { "Gi0/0": { description: "new branch" } } };
  await panel.historyPanel.push(branched, "CONFIG_INT", "Edit after undo");
  assert.equal(redo.disabled, true);
  assert.deepEqual(normalize(panel.draftSnapshot), branched);
  await assert.rejects(panel.historyPanel.restore({ backup_version: "invalid" }), /Invalid history/);
  assert.deepEqual(normalize(panel.draftSnapshot), branched);
  assert.equal(message.hidden, false);
  await panel.historyPanel.refresh();
  assert.equal(message.hidden, true); assert.equal(undo.disabled, false);

  const audit = (await panel.historyPanel.exportBackup()).audit_trail;
  assert.ok(audit.some(e => e.action_type === "RESTORE" && e.actor === "actor-1"));
  assert.deepEqual(normalize([panel.pendingChanges, panel.pendingBatches, panel.changesById, panel.batchesById]), originalPending);
  assert.equal(events.at(-1)[0], "nc-history-snapshot");
  assert.deepEqual(events.at(-1)[1].snapshot, branched);
  panel.destroy();
  assert.equal(timers.size, 0); assert.equal(undo.disabled, true); assert.equal(redo.disabled, true);
  await assert.rejects(panel.historyPanel.exportBackup(), /destroyed/);
  process.stdout.write(JSON.stringify({ done: true, requests: sequence }) + "\n");
})().catch(error => { console.error(error); process.exitCode = 1; })
  .finally(() => { clearTimeout(watchdog); lines.close(); });
