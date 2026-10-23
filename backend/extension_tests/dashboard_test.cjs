const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function createDashboard(handler, initial = {}) {
  let factory;
  const storage = new Map(Object.entries(initial));
  const requests = [];
  const redirects = [];
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../src/network_copilot/static/js/dashboard.js"), "utf8"), {
    document: { addEventListener: (_, callback) => callback() },
    Alpine: { data: (_, callback) => { factory = callback; } },
    localStorage: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: key => storage.delete(key),
    },
    window: { location: { assign: url => redirects.push(url) } },
    setInterval: () => 1,
    clearInterval: () => {},
    fetch: async (url, options) => {
      requests.push({ url, options });
      const result = await handler(url, options);
      return { ok: true, status: 200, json: async () => result };
    },
  });
  return { app: factory(), storage, requests, redirects };
}

(async () => {
  const projects = [{ id: 1, name: "Default Lab" }, { id: 2, name: "Second Lab" }];
  const state = createDashboard((url, options) => {
    if (url === "/api/projects") return { items: projects };
    if (url === "/api/dashboard/summary") return { project: options.headers["X-Project-Id"] };
    throw new Error(url);
  }, { nc_token: "session", nc_user: JSON.stringify({ role: "ADMIN" }), nc_project_id: "999" });
  await state.app.init();
  assert.equal(state.app.currentProjectId, 1);
  assert.equal(state.app.summary.project, "1");
  await state.app.selectProject(2);
  assert.equal(state.app.summary.project, "2");
  assert.equal(state.app.projectName, "Second Lab");
  assert.equal(state.app.activityActionLabel("project.update"), "Cập nhật dự án");
  assert.equal(state.app.activityActionLabel("ai.action"), "Thao tác bằng AI");
  assert.equal(state.app.activityActionLabel("topology.link_create"), "Tạo kết nối");
  assert.equal(state.app.activityActionLabel("device.create"), "Thêm thiết bị");
  assert.equal(state.app.activityActionLabel("custom.thing_create"), "Custom Thing Create");

  const empty = createDashboard(() => ({ items: [] }), { nc_token: "session" });
  await empty.app.init();
  assert.equal(empty.app.noProject, true);
  assert.equal(empty.requests.length, 1, "No summary request without a project");

  const pending = createDashboard(() => ({
    access_token: "pending", user: { role: "ADMIN" }, mfa_setup_required: true,
  }), { nc_token: "expired" });
  await pending.app.login();
  assert.equal(pending.requests[0].options.headers.Authorization, undefined);
  assert.equal(pending.redirects[0], "/");
  assert.equal(pending.requests.length, 1);

  const template = fs.readFileSync(path.join(__dirname, "../src/network_copilot/templates/dashboard.html"), "utf8");
  assert.ok(template.indexOf('<template x-if="summary">') < template.indexOf('Object.entries(summary.devices.by_role)'));
  console.log("Dashboard project selection and initialization: passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
