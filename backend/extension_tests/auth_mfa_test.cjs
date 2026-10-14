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
  const user = { id: 1, role: "ADMIN", mfa_enabled: false };
  const state = createApp(url => {
    if (url === "/api/auth/login") return { access_token: "pending", user, mfa_setup_required: true };
    if (url === "/api/auth/mfa/setup") return { secret: "trial-secret" };
    if (url === "/api/auth/mfa/enable") return { access_token: "full", recovery_codes: ["trial-recovery"] };
    throw Error("Unexpected request: " + url);
  }, { nc_token: "expired" });
  state.app.loginForm = { username: "admin", password: "trial-password", code: "", recovery_code: "" };
  await state.app.login();
  assert.equal(state.requests[0].options.headers.Authorization, undefined, "Login must not send an expired session token");
  assert.equal(state.app.authBlocked, true);
  assert.equal(state.bootstraps(), 0, "Pending MFA must not request protected app data");
  state.app.mfaForm.password = "trial-password";
  await state.app.setupMfa();
  assert.equal(state.app.mfaSecret, "trial-secret");
  assert.equal(state.app.mfaForm.password, "");
  state.app.mfaForm.code = "123456";
  await state.app.enableMfa();
  assert.equal(state.app.token, "full");
  assert.equal(state.app.currentUser.mfa_enabled, true);
  assert.equal(state.app.authBlocked, true, "Recovery codes must be displayed before app bootstrap");
  await state.app.finishMfa();
  assert.equal(state.app.authBlocked, false);
  assert.equal(state.bootstraps(), 5);
  state.app.logout();
  assert.equal(state.app.mfaSecret, "");
  assert.equal(state.app.mfaRecoveryCodes.length, 0);

  const resumed = createApp(() => user, { nc_token: "pending", nc_user: JSON.stringify(user) });
  await resumed.app.init();
  assert.equal(resumed.app.mfaSetupRequired, true);
  assert.equal(resumed.bootstraps(), 0, "Reloading a pending session must return to enrollment");

  const enrolled = createApp((_, options) => {
    assert.equal(JSON.parse(options.body).code, "654321");
    return { access_token: "full", user: { ...user, mfa_enabled: true }, mfa_setup_required: false };
  });
  enrolled.app.loginForm.code = "654321";
  await enrolled.app.login();
  assert.equal(enrolled.bootstraps(), 5);
  console.log("Auth MFA flows: passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
