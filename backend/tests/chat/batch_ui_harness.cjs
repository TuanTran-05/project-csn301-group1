const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const appSource = fs.readFileSync(
  path.resolve(__dirname, "../../src/network_copilot/static/js/app.js"),
  "utf8"
);

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

function makeChange(id, batchId, hostname, status = "pending_approval") {
  const wasApproved = !["pending_approval", "cancelled"].includes(status);
  return {
    id,
    status,
    risk_level: "high",
    requires_confirmation: true,
    description: "Save configuration",
    device: {
      id,
      hostname,
      management_ip: `10.10.10.${id}`,
      role: "access",
      device_type: "cisco_ios",
    },
    commands: ["write memory"],
    verification_commands: [],
    rollback_commands: [],
    warnings: [],
    requested_by_id: 1,
    approved_by_id: wasApproved ? 1 : null,
    batch_id: batchId,
    execution_mode: "exec",
    backup_id: null,
    apply_output: null,
    verification_output: null,
    error_message: null,
    source: "ai",
    created_at: "2026-08-01T00:00:00+00:00",
    approved_at: wasApproved ? "2026-08-01T00:01:00+00:00" : null,
    applied_at: status === "success" ? "2026-08-01T00:02:00+00:00" : null,
  };
}

function makeBatch(id, status = "pending_approval", confirmationText = "CONFIRM ALL") {
  const wasApproved = !["pending_approval", "cancelled"].includes(status);
  const changes = confirmationText === "CONFIRM ALL"
    ? [
        makeChange(id * 10 + 1, id, `ACC-SW${id}`, status),
        makeChange(id * 10 + 2, id, `DIST-SW${id}`, status),
      ]
    : [makeChange(id * 10 + 1, id, confirmationText, status)];
  return {
    id,
    status,
    risk_level: "high",
    requires_confirmation: true,
    description: "Save configurations",
    source: "ai",
    requested_by_id: 1,
    approved_by_id: wasApproved ? 1 : null,
    confirmation_text: confirmationText,
    created_at: "2026-08-01T00:00:00+00:00",
    approved_at: wasApproved ? "2026-08-01T00:01:00+00:00" : null,
    applied_at: status === "success" ? "2026-08-01T00:02:00+00:00" : null,
    changes,
  };
}

function createApp() {
  let factory = null;
  let timerId = 0;
  const clearedIntervals = [];
  const storage = new Map();
  const sandbox = {
    Alpine: {
      data(name, builder) {
        assert.equal(name, "app");
        factory = builder;
      },
    },
    document: {
      addEventListener(name, callback) {
        assert.equal(name, "alpine:init");
        callback();
      },
    },
    localStorage: {
      getItem(key) {
        return storage.has(key) ? storage.get(key) : null;
      },
      setItem(key, value) {
        storage.set(key, value);
      },
      removeItem(key) {
        storage.delete(key);
      },
    },
    setInterval() {
      timerId += 1;
      return timerId;
    },
    clearInterval(id) {
      clearedIntervals.push(id);
    },
    setTimeout() {
      timerId += 1;
      return timerId;
    },
    clearTimeout() {},
    fetch() {
      throw new Error("Unexpected real fetch in batch UI harness");
    },
    alert() {},
    console,
  };

  vm.runInNewContext(appSource, sandbox, { filename: "app.js" });
  assert.equal(typeof factory, "function");
  const app = factory();
  app.$refs = {};
  app.$nextTick = (callback) => callback();
  return { app, clearedIntervals };
}

async function staleChatSnapshot() {
  const { app } = createApp();
  app.batchesById[1] = makeBatch(1, "approved");

  app._ingestMessage({
    id: 10,
    user_id: 1,
    username: "admin",
    role: "assistant",
    content: "Batch preview created.",
    payload: { batch: makeBatch(1, "pending_approval") },
    created_at: "2026-08-01T00:00:00+00:00",
  });

  assert.equal(app.batchesById[1].status, "approved");
}

async function staleGetAfterAction() {
  const { app } = createApp();
  const getResponse = deferred();
  const actionResponse = deferred();
  app.batchesById[1] = makeBatch(1, "pending_approval");
  app.authFetch = (url) => {
    if (url === "/api/change-batches?limit=500") return getResponse.promise;
    if (url === "/api/change-batches/1/approve") return actionResponse.promise;
    throw new Error(`Unexpected URL: ${url}`);
  };

  const refresh = app.refreshBatches();
  const action = app.approveBatch(1);
  actionResponse.resolve(makeBatch(1, "approved"));
  await action;
  getResponse.resolve({ items: [makeBatch(1, "pending_approval")] });
  await refresh;

  assert.equal(app.batchesById[1].status, "approved");
}

async function latestRefreshWins() {
  const { app } = createApp();
  const firstResponse = deferred();
  const secondResponse = deferred();
  let requestCount = 0;
  app.authFetch = (url) => {
    assert.equal(url, "/api/change-batches?limit=500");
    requestCount += 1;
    return requestCount === 1 ? firstResponse.promise : secondResponse.promise;
  };

  const first = app.refreshBatches();
  const second = app.refreshBatches();
  secondResponse.resolve({ items: [makeBatch(1, "approved")] });
  await second;
  firstResponse.resolve({ items: [makeBatch(1, "pending_approval")] });
  await first;

  assert.equal(app.batchesById[1].status, "approved");
}

async function refreshSkipsWhileActionRuns() {
  const { app } = createApp();
  const actionResponse = deferred();
  let getRequests = 0;
  app.batchesById[1] = makeBatch(1, "pending_approval");
  app.authFetch = (url) => {
    if (url === "/api/change-batches/1/approve") return actionResponse.promise;
    if (url === "/api/change-batches?limit=500") {
      getRequests += 1;
      return Promise.resolve({ items: [] });
    }
    throw new Error(`Unexpected URL: ${url}`);
  };

  const action = app.approveBatch(1);
  await app.refreshBatches();
  assert.equal(getRequests, 0);
  actionResponse.resolve(makeBatch(1, "approved"));
  await action;
}

async function actionsLockPerBatch() {
  const { app } = createApp();
  const responses = { 1: deferred(), 2: deferred() };
  const calls = [];
  app.batchesById[1] = makeBatch(1, "pending_approval");
  app.batchesById[2] = makeBatch(2, "pending_approval");
  app.authFetch = (url) => {
    const match = url.match(/^\/api\/change-batches\/(\d+)\/approve$/);
    assert.ok(match, `Unexpected URL: ${url}`);
    const id = Number(match[1]);
    calls.push(id);
    return responses[id].promise;
  };

  const first = app.approveBatch(1);
  const duplicate = app.approveBatch(1);
  const other = app.approveBatch(2);
  assert.deepEqual(calls, [1, 2]);
  responses[1].resolve(makeBatch(1, "approved"));
  responses[2].resolve(makeBatch(2, "approved"));
  await Promise.all([first, duplicate, other]);
  assert.equal(app.batchesById[1].status, "approved");
  assert.equal(app.batchesById[2].status, "approved");
}

async function confirmationIsExactAndUntrimmed() {
  const { app } = createApp();
  let submittedBody = null;
  app.batchesById[1] = makeBatch(1, "approved", "CONFIRM ALL");
  app.batchConfirmInputs[1] = " CONFIRM ALL ";
  assert.equal(app.batchConfirmationMatches(1), false);
  app.batchConfirmInputs[1] = "CONFIRM ALL";
  assert.equal(app.batchConfirmationMatches(1), true);
  app.authFetch = (url, options) => {
    assert.equal(url, "/api/change-batches/1/apply");
    submittedBody = JSON.parse(options.body);
    return Promise.resolve(makeBatch(1, "success", "CONFIRM ALL"));
  };

  await app.applyBatch(1);
  assert.deepEqual(submittedBody, { confirmation: "CONFIRM ALL" });
}

async function verificationPlanIsAvailableBeforeApproval() {
  const { app } = createApp();
  const batch = makeBatch(1, "pending_approval");
  const child = batch.changes[0];
  child.verification_commands = ["show startup-config", "show vlan brief"];
  app.batchesById[batch.id] = batch;

  assert.deepEqual(plain(app.batchVerificationPlan(child)), [
    "show startup-config",
    "show vlan brief",
  ]);
  assert.deepEqual(plain(app.batchVerificationResults(child)), []);
}

async function verificationResultsAppearAfterApply() {
  const { app } = createApp();
  const batch = makeBatch(1, "approved");
  app.batchesById[batch.id] = batch;
  app.batchConfirmInputs[batch.id] = "CONFIRM ALL";

  const applied = makeBatch(1, "partial_success");
  applied.changes[0].status = "success";
  applied.changes[0].verification_output = {
    "show startup-config": {
      passed: true,
      details: ["Startup configuration verified."],
      output: "startup configuration is present",
    },
    "show vlan brief": {
      passed: false,
      details: ["Expected VLAN 25 was not found."],
      output: "VLAN 10 active",
    },
    "show running-config": {
      passed: true,
      details: ["Sensitive configuration was withheld."],
      output: "username admin secret 5 sensitive-value",
      redacted: true,
    },
  };
  applied.changes[1].status = "failed";
  applied.changes[1].verification_output = {
    "show interfaces": {
      passed: false,
      details: ["Interface Gi0/1 remains down."],
      output: "Gi0/1 is administratively down",
    },
  };
  app.authFetch = (url) => {
    assert.equal(url, "/api/change-batches/1/apply");
    return Promise.resolve(applied);
  };

  await app.applyBatch(1);

  assert.deepEqual(plain(app.batchVerificationResults(app.batchesById[1].changes[0])), [
    {
      command: "show startup-config",
      status: "passed",
      details: ["Startup configuration verified."],
      output: "startup configuration is present",
      redacted: false,
    },
    {
      command: "show vlan brief",
      status: "failed",
      details: ["Expected VLAN 25 was not found."],
      output: "VLAN 10 active",
      redacted: false,
    },
    {
      command: "show running-config",
      status: "passed",
      details: ["Sensitive configuration was withheld."],
      output: "Verification output redacted for safety.",
      redacted: true,
    },
  ]);
  assert.deepEqual(plain(app.batchVerificationResults(app.batchesById[1].changes[1])), [
    {
      command: "show interfaces",
      status: "failed",
      details: ["Interface Gi0/1 remains down."],
      output: "Gi0/1 is administratively down",
      redacted: false,
    },
  ]);
}

async function logoutInvalidatesActionAndCleansTimers() {
  const { app, clearedIntervals } = createApp();
  const actionResponse = deferred();
  app.token = "token";
  app.currentUser = { id: 1, username: "admin", role: "ADMIN" };
  app.batchesById[1] = makeBatch(1, "pending_approval");
  app._deviceTimer = 11;
  app._changesTimer = 12;
  app._batchesTimer = 13;
  app._messagesTimer = 14;
  app.authFetch = () => actionResponse.promise;

  const action = app.approveBatch(1);
  app.logout();
  actionResponse.resolve(makeBatch(1, "approved"));
  await action;

  assert.deepEqual(Object.keys(app.batchesById), []);
  assert.deepEqual(Object.keys(app.batchActionIds), []);
  assert.equal(app.currentUser, null);
  assert.deepEqual(clearedIntervals, [11, 12, 13, 14]);
}

const cases = {
  stale_chat_snapshot: staleChatSnapshot,
  stale_get_after_action: staleGetAfterAction,
  latest_refresh_wins: latestRefreshWins,
  refresh_skips_during_action: refreshSkipsWhileActionRuns,
  actions_lock_per_batch: actionsLockPerBatch,
  confirmation_exact: confirmationIsExactAndUntrimmed,
  verification_plan_before_approval: verificationPlanIsAvailableBeforeApproval,
  verification_results_after_apply: verificationResultsAppearAfterApply,
  logout_cleanup: logoutInvalidatesActionAndCleansTimers,
};

async function main() {
  const caseName = process.argv[2];
  assert.ok(cases[caseName], `Unknown harness case: ${caseName}`);
  await cases[caseName]();
  process.stdout.write(JSON.stringify({ case: caseName, ok: true }));
}

main().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
