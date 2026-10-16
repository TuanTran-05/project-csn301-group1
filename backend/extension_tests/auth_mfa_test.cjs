const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function createApp(handler, stored = {}) {
  let factory;
  const storage = new Map(Object.entries(stored));
  const requests = [];
  const context = {
    document: { addEventListener: (_, callback) => callback() },
    Alpine: { data: (_, callback) => { factory = callback; } },
    localStorage: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: key => storage.delete(key),
    },
    clearTimeout, setTimeout,
    fetch: async (url, options) => {
      requests.push({ url, options });
      const data = await handler(url, options);
      return { ok: true, status: 200, json: async () => data };
    },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../src/network_copilot/static/js/app.js"), "utf8"), context);
  const app = factory();
  let bootstraps = 0;
  for (const method of ["loadSessions", "hydrateMessages", "refreshDevices", "refreshChanges", "refreshBatches"])
    app[method] = async () => { bootstraps++; };
  app.startPolling = () => {};
  app.startRealtime = () => {};
  app.loadProjects = async () => {};
  app.stopPolling = () => {};
  return { app, requests, storage, bootstraps: () => bootstraps };
}

(async () => {
  for (const mfa_enabled of [undefined, false, true]) {
    const user = { id: 1, role: "ADMIN", mfa_enabled };
    const state = createApp((url, options) => {
      assert.equal(url, "/api/auth/login");
      assert.deepEqual(JSON.parse(options.body), { username: "admin", password: "trial-password" });
      return { access_token: "full", user, mfa_setup_required: false };
    }, { nc_token: "expired" });
    state.app.loginForm = { username: "admin", password: "trial-password" };
    await state.app.login();
    assert.equal(state.app.loginError, "");
    assert.equal(state.requests[0].options.headers.Authorization, undefined);
    assert.equal(state.bootstraps(), 5, "Password login must load the app immediately");
    const resumed = createApp(() => user, { nc_token: "full", nc_user: JSON.stringify(user) });
    await resumed.app.init();
    assert.equal(resumed.bootstraps(), 5, "Reload must not require MFA enrollment");
    state.app.logout();
    assert.equal(state.app.currentUser, null);
  }
  console.log("Password-only admin login and reload: passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
