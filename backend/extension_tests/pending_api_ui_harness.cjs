/* Actual app.js refresh/getters + pending controller + Flask test client via pipes. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const readline = require("node:readline");
const [deviceId, batchId, userId] = process.argv.slice(2).map(Number);
const lines = readline.createInterface({ input: process.stdin });
const pending = new Map();
let sequence = 0;
const watchdog = setTimeout(() => { console.error("Pending UI test timed out"); process.exit(3); }, 30000);
lines.on("line", line => {
  const reply = JSON.parse(line), handlers = pending.get(reply.id);
  pending.delete(reply.id);
  if (reply.status >= 400) {
    const error = new Error(reply.data.message || "API failed"); error.status = reply.status; handlers.reject(error);
  } else handlers.resolve(reply.data);
});
function request(url, options = {}) {
  return new Promise((resolve, reject) => {
    pending.set(++sequence, { resolve, reject });
    process.stdout.write(JSON.stringify({ id: sequence, url, options }) + "\n");
  });
}
const post = data => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
function element() {
  return { disabled: true, hidden: true, listeners: {}, addEventListener(name, fn) { this.listeners[name] = fn; },
    removeEventListener(name) { delete this.listeners[name]; }, setAttribute() {} };
}
const undo = element(), redo = element(), message = element(), toolbar = element();
const root = { querySelector(s) { return s.includes('"undo"') ? undo : s.includes('"redo"') ? redo : s.includes("message") ? message : toolbar; } };
const callbacks = [], factories = {}, timers = new Set();
const context = { window: {}, Error, JSON, encodeURIComponent, alert(text) { throw Error(text); },
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  document: { addEventListener(name, fn) { callbacks.push(fn); } },
  Alpine: { data(name, factory) { factories[name] = factory; } },
  setInterval(fn) { timers.add(fn); return fn; }, clearInterval(fn) { timers.delete(fn); },
};
for (const file of ["app.js", "history-toolbar.js", "history-panel.js"]) {
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../src/network_copilot/static/js", file), "utf8"), context);
  context.NetAIHistory = context.window.NetAIHistory;
}
callbacks.forEach(fn => fn());
(async () => {
  // Preserve property descriptors so the real app's pending getters run unchanged.
  const panel = factories.app();
  Object.defineProperties(panel, Object.getOwnPropertyDescriptors(factories.historyDraft()));
  Object.assign(panel, { token: "test-transport", currentUser: { id: userId, role: "ADMIN" }, $el: root,
    $watch() {}, $dispatch() {}, authFetch: request });
  panel.connectHistory();
  while (panel.draftSnapshot === null) await new Promise(resolve => setTimeout(resolve, 5));
  assert.equal(panel.pendingChanges.length, 0); assert.equal(panel.pendingBatches.length, 1);
  const create = async description => request("/api/changes/preview", post({ device_id: deviceId,
    commands: ["interface Gi0/2", "description " + description], description }));
  const first = await create("first");
  await panel.historyPanel.refresh();
  assert.equal(panel.pendingChanges.length, 1); assert.equal(undo.disabled, false);
  const second = await create("second");
  await panel.historyPanel.refresh();
  assert.equal(panel.pendingChanges.length, 2);

  // A late poll from before Undo must not overwrite canonical post-Undo state.
  const staleData = await request("/api/changes?standalone_only=true&limit=500");
  let release;
  panel.authFetch = (url, options) => {
    if (!release && url.startsWith("/api/changes?")) return new Promise(resolve => { release = resolve; });
    return request(url, options);
  };
  const stalePoll = panel.refreshChanges();
  await undo.listeners.click();
  assert.equal(panel.pendingChanges.length, 1);
  release(staleData); await stalePoll;
  assert.equal(panel.pendingChanges.length, 1);
  panel.authFetch = request;
  assert.equal(panel.changesById[second.id].status, "cancelled");
  await redo.listeners.click();
  assert.equal(panel.pendingChanges.length, 2);

  // Two editors read one revision. A stale UI keeps unsaved input after 409.
  await panel.openPendingEditor("change", first.id);
  const staleEditor = panel.pendingEditor;
  const anotherEditor = await request(`/api/pending-history/changes/${first.id}/editor`);
  assert.equal(staleEditor.revision, anotherEditor.revision);
  await request(`/api/pending-history/changes/${first.id}`, { ...post({
    revision: anotherEditor.revision, description: "saved by another editor" }), method: "PATCH" });
  staleEditor.changes[0].description = "unsaved stale editor";
  await panel.savePendingEditor();
  assert.equal(panel.pendingEditor, staleEditor);
  assert.equal(panel.pendingEditor.changes[0].description, "unsaved stale editor");
  assert.match(panel.pendingEditorError, /reopen the editor/);
  assert.equal((await request(`/api/changes/${first.id}`)).description, "saved by another editor");
  await panel.historyPanel.refresh();

  for (const description of ["edited", "edited again"]) {
    await panel.openPendingEditor("change", first.id);
    assert.ok(panel.pendingEditor);
    panel.pendingEditor.changes[0].description = description;
    panel.pendingEditor.changes[0].commandsText = "interface Gi0/2\ndescription " + description;
    await panel.savePendingEditor();
    assert.equal(panel.pendingEditor, null, panel.pendingEditorError);
    assert.equal(panel.changesById[first.id].description, description);
  }
  await undo.listeners.click();
  assert.equal(panel.changesById[first.id].description, "edited");
  const backup = await panel.historyPanel.exportBackup();
  await redo.listeners.click();
  assert.equal(panel.changesById[first.id].description, "edited again");
  await panel.historyPanel.restore(backup);
  assert.equal(panel.changesById[first.id].description, "edited");
  assert.equal(redo.disabled, false);
  const third = await create("branch");
  await panel.historyPanel.refresh();
  assert.equal(redo.disabled, true);
  assert.ok(panel.pendingChanges.some(row => row.id === third.id));

  await panel.openPendingEditor("batch", batchId);
  assert.equal(panel.pendingEditor.changes.length, 2);
  panel.pendingEditor.description = "batch edited";
  for (const child of panel.pendingEditor.changes) {
    child.description = "batch edited"; child.commandsText = "interface Gi0/2\ndescription batch-edited";
  }
  await panel.savePendingEditor();
  assert.equal(panel.pendingEditor, null, panel.pendingEditorError);
  assert.ok(panel.batchesById[batchId].changes.every(c => c.description === "batch edited"));
  await undo.listeners.click();
  assert.ok(panel.batchesById[batchId].changes.every(c => c.description === "first"));
  await redo.listeners.click();
  assert.ok(panel.batchesById[batchId].changes.every(c => c.description === "batch edited"));
  await request(`/api/change-batches/${batchId}/cancel`, { method: "POST" });
  await panel.historyPanel.refresh();
  assert.equal(panel.pendingBatches.length, 0);
  await undo.listeners.click();
  assert.equal(panel.pendingBatches.length, 1);
  assert.ok(panel.batchesById[batchId].changes.every(c => c.status === "pending_approval"));
  panel.destroy();
  assert.equal(timers.size, 0);
  process.stdout.write(JSON.stringify({ done: true, requests: sequence, pendingIds: panel.pendingChanges.map(c => c.id) }) + "\n");
})().catch(error => { console.error(error); process.exitCode = 1; })
  .finally(() => { clearTimeout(watchdog); lines.close(); });
