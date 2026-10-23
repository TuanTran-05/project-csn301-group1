const assert = require("node:assert/strict");
const fs = require("node:fs");
const ui = require("../src/network_copilot/static/js/lab_bootstrap_demo.js");

const uiSource = fs.readFileSync(require.resolve("../src/network_copilot/static/js/lab_bootstrap_demo.js"), "utf8");
assert.doesNotMatch(uiSource, /\b(?:localStorage|sessionStorage)\b/);

class FakeElement {
  constructor(ownerDocument) {
    this.ownerDocument = ownerDocument;
    this.hidden = false;
    this.textContent = "";
    this.children = [];
    this.listeners = {};
    this.value = "";
    this.files = [];
    this.disabled = false;
    this.className = "";
  }
  replaceChildren() { this.children = []; }
  appendChild(child) { this.children.push(child); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
}

class FakeDocument {
  constructor() { this.cookie = ""; }
  querySelector(selector) { return selector === "#lab-bootstrap-demo" ? this.root : null; }
  createElement() { return new FakeElement(this); }
}

class FakeRoot extends FakeElement {
  constructor() {
    const document = new FakeDocument();
    super(document);
    document.root = this;
    this.dataset = { authMode: "header", csrfCookieName: "lab_bootstrap_demo_csrf" };
    this.elements = new Map();
    [
      "#archive-form", "#archive-file", "#access-token", "#analyze-button", "#request-status",
      "#analysis-results", "#analysis-status", "#preview-compatible", "#planner-status",
      "#gateway-status", "#device-actions", "#archive-sha256 code", "#nodes",
      "#segments", "#warnings", "#issues", "#planner-blockers",
      "#export-inspection-button", "#export-status"
    ].forEach((selector) => this.elements.set(selector, new FakeElement(document)));
  }
  querySelector(selector) { return this.elements.get(selector); }
}

const response = {
  status: "ANALYZED_UNVERIFIED_SCHEMA",
  preview_compatible: false,
  planner_eligible: false,
  planner_status: "NOT_RUN",
  planner_blockers: ["The .unl schema has not been verified for planner use."],
  gateway_status: "NOT_SUPPORTED",
  device_actions: "NOT_PERFORMED",
  analysis: {
    status: "ANALYZED_UNVERIFIED_SCHEMA",
    preview_compatible: false,
    warnings: ["Archive analysis is not connected to IP planning."],
    source_archive_sha256: "a".repeat(64),
    nodes: [{
      id: "1", name: "vIOS1", template: "vios", image: "image-a", image_build: null,
      interfaces: [{ id: "0", name: "Gi0/0" }]
    }]
  },
  inspection: {
    planner_eligible: false,
    segments: [{
      id: "network:1", kind: "network",
      endpoints: [{ node_id: "1", interface: "Gi0/0" }, { node_id: "2", interface: "Gi0/0" }]
    }],
    blockers: ["The .unl schema has not been verified for planner use."],
    issues: ["A metadata issue was reported."]
  }
};

const view = ui.toViewModel(response);
assert.equal(view.status, "ANALYZED_UNVERIFIED_SCHEMA");
assert.equal(view.previewCompatible, false);
assert.equal(view.plannerEligible, false);
assert.equal(view.gatewayStatus, "NOT_SUPPORTED");
assert.equal(view.warnings.length, 1);
assert.equal(view.issues.length, 1);
assert.equal(view.plannerBlockers.length, 1);

const root = new FakeRoot();
ui.renderResult(root, response);
assert.equal(root.querySelector("#analysis-results").hidden, false);
assert.equal(root.querySelector("#analysis-status").textContent, "ANALYZED_UNVERIFIED_SCHEMA");
assert.equal(root.querySelector("#preview-compatible").textContent, "false");
assert.match(root.querySelector("#planner-status").textContent, /planner bị khóa/);
assert.equal(root.querySelector("#gateway-status").textContent, "NOT_SUPPORTED");
assert.match(root.querySelector("#nodes").children[0].textContent, /image_build:/);
assert.match(root.querySelector("#segments").children[0].textContent, /network:1 \[network\]/);
assert.equal(root.querySelector("#warnings").children[0].textContent, response.analysis.warnings[0]);
assert.equal(root.querySelector("#issues").children[0].textContent, response.inspection.issues[0]);
assert.equal(root.querySelector("#planner-blockers").children[0].textContent, response.planner_blockers[0]);
assert.equal(root.querySelector("#archive-sha256 code").textContent, "a".repeat(64));

assert.throws(
  () => ui.toViewModel({ ...response, gateway_status: "SUPPORTED" }),
  /schema Safe Demo/
);
assert.throws(
  () => ui.toViewModel({ ...response, preview_compatible: true }),
  /schema Safe Demo/
);
assert.throws(
  () => ui.toViewModel({ ...response, analysis: { ...response.analysis, preview_compatible: true } }),
  /schema Safe Demo/
);
assert.throws(
  () => ui.toViewModel({ ...response, planner_status: "PLANNED", planner_eligible: true }),
  /schema Safe Demo/
);
assert.throws(
  () => ui.toViewModel({ ...response, device_actions: "PERFORMED" }),
  /schema Safe Demo/
);
assert.throws(
  () => ui.toViewModel({ ...response, status: "ANALYZED_VERIFIED_SCHEMA" }),
  /schema Safe Demo/
);

async function testManualJwtIsUsedWithoutPersistingIt() {
  const root = new FakeRoot();
  root.querySelector("#archive-file").files = [{ size: 12 }];
  root.querySelector("#access-token").value = "pasted-demo-jwt";
  let request;
  const fetchImpl = async (path, options) => {
    request = { path, options };
    return { ok: true, json: async () => response };
  };

  ui.mount(root.ownerDocument, fetchImpl);

  await root.querySelector("#archive-form").listeners.submit({ preventDefault() {} });

  assert.equal(request.path, "/api/lab-bootstrap-demo/analyze-archive");
  assert.equal(request.options.headers.Authorization, "Bearer pasted-demo-jwt");
  assert.equal(request.options.credentials, "omit");
  assert.equal(root.querySelector("#access-token").value, "pasted-demo-jwt");
}

async function testRunnerCookieJwtUsesCsrfWithoutExposingBearer() {
  const root = new FakeRoot();
  root.dataset.authMode = "cookie";
  root.ownerDocument.cookie = "lab_bootstrap_demo_csrf=csrf-value";
  root.querySelector("#archive-file").files = [{ size: 12 }];
  let request;
  const fetchImpl = async (path, options) => {
    request = { path, options };
    return { ok: true, json: async () => response };
  };

  ui.mount(root.ownerDocument, fetchImpl);
  await root.querySelector("#archive-form").listeners.submit({ preventDefault() {} });

  assert.equal(request.path, "/api/lab-bootstrap-demo/analyze-archive");
  assert.equal(request.options.headers.Authorization, undefined);
  assert.equal(request.options.headers["X-CSRF-TOKEN"], "csrf-value");
  assert.equal(request.options.credentials, "same-origin");
}

async function testUnsupportedSchemaAndIncompleteTopologyErrorsAreClear() {
  for (const [error, message, expected] of [
    ["unsupported_schema", "The .unl XML root must be <lab>.", /Unsupported PNETLab schema/],
    ["incomplete_topology", "The .unl topology does not contain any nodes.", /missing required data/],
    ["invalid_archive", "The uploaded file is not a valid, readable ZIP archive.", /Invalid archive/]
  ]) {
    const root = new FakeRoot();
    root.querySelector("#archive-file").files = [{ size: 12 }];
    root.querySelector("#access-token").value = "fixture-jwt";
    const fetchImpl = async () => ({
      ok: false,
      json: async () => ({ error, message })
    });
    ui.mount(root.ownerDocument, fetchImpl);
    await root.querySelector("#archive-form").listeners.submit({ preventDefault() {} });
    assert.match(root.querySelector("#request-status").textContent, expected);
    assert.equal(root.querySelector("#request-status").className, "error");
    assert.equal(root.querySelector("#analyze-button").disabled, false);
  }
}

async function testLoadingAndSuccessStatesAreReflected() {
  const root = new FakeRoot();
  root.querySelector("#archive-file").files = [{ size: 12 }];
  root.querySelector("#access-token").value = "fixture-jwt";
  let finishRequest;
  const fetchImpl = () => new Promise((resolve) => { finishRequest = resolve; });
  ui.mount(root.ownerDocument, fetchImpl);
  const submission = root.querySelector("#archive-form").listeners.submit({ preventDefault() {} });
  await Promise.resolve();
  assert.equal(root.querySelector("#analyze-button").disabled, true);
  assert.notEqual(root.querySelector("#request-status").textContent, "");
  finishRequest({ ok: true, json: async () => response });
  await submission;
  assert.equal(root.querySelector("#analyze-button").disabled, false);
  assert.equal(root.querySelector("#analysis-results").hidden, false);
}

async function testReviewOnlyExportRequiresCleanInspectionAndNeverClaimsPlanning() {
  const rejectedRoot = new FakeRoot();
  rejectedRoot.querySelector("#archive-file").files = [{ size: 12 }];
  rejectedRoot.querySelector("#access-token").value = "fixture-jwt";
  ui.mount(rejectedRoot.ownerDocument, async () => ({ ok: true, json: async () => response }));
  await rejectedRoot.querySelector("#archive-form").listeners.submit({ preventDefault() {} });
  assert.equal(rejectedRoot.querySelector("#export-inspection-button").hidden, true);
  assert.equal(rejectedRoot.querySelector("#export-inspection-button").disabled, true);
  assert.match(rejectedRoot.querySelector("#planner-status").textContent, /NOT_RUN|planner bị khóa/);

  const acceptedResponse = JSON.parse(JSON.stringify(response));
  acceptedResponse.inspection.issues = [];
  const root = new FakeRoot();
  root.dataset.authMode = "cookie";
  root.ownerDocument.cookie = "lab_bootstrap_demo_csrf=csrf-value";
  const file = { size: 12, name: "fixture.zip" };
  root.querySelector("#archive-file").files = [file];
  const requests = [];
  const downloads = [];
  const fetchImpl = async (path, options) => {
    requests.push({ path, options });
    if (path === "/api/lab-bootstrap-demo/analyze-archive") {
      return { ok: true, json: async () => acceptedResponse };
    }
    return {
      ok: true,
      headers: { get: (name) => name === "Content-Type" ? "application/zip" : null },
      blob: async () => "fixture-report-blob"
    };
  };
  ui.mount(root.ownerDocument, fetchImpl, async (blob, filename) => downloads.push({ blob, filename }));
  await root.querySelector("#archive-form").listeners.submit({ preventDefault() {} });

  const exportButton = root.querySelector("#export-inspection-button");
  assert.equal(exportButton.hidden, false);
  assert.equal(exportButton.disabled, false);
  assert.match(root.querySelector("#export-status").textContent, /review-only/);
  assert.equal(root.querySelector("#planner-status").textContent.startsWith("NOT_RUN"), true);

  await exportButton.listeners.click({ preventDefault() {} });
  assert.equal(requests.length, 2);
  assert.equal(requests[1].path, "/api/lab-bootstrap-demo/export-inspection");
  assert.equal(requests[1].options.method, "POST");
  assert.equal(requests[1].options.body, file);
  assert.equal(requests[1].options.headers["X-CSRF-TOKEN"], "csrf-value");
  assert.equal(requests[1].options.headers.Authorization, undefined);
  assert.equal(requests[1].options.credentials, "same-origin");
  assert.deepEqual(downloads, [{ blob: "fixture-report-blob", filename: "lab-bootstrap-inspection-report.zip" }]);
  assert.match(root.querySelector("#export-status").textContent, /không phải IP plan/);
  assert.equal(root.querySelector("#planner-status").textContent.startsWith("NOT_RUN"), true);
}

async function testChangingSelectedFileDoesNotReuseStaleInspectionOrExport() {
  const cleanResponse = JSON.parse(JSON.stringify(response));
  cleanResponse.inspection.issues = [];

  const analysisRoot = new FakeRoot();
  analysisRoot.querySelector("#archive-file").files = [{ name: "first.zip", size: 12 }];
  analysisRoot.querySelector("#access-token").value = "fixture-jwt";
  let finishAnalysis;
  ui.mount(analysisRoot.ownerDocument, () => new Promise((resolve) => { finishAnalysis = resolve; }));
  const analysisSubmission = analysisRoot.querySelector("#archive-form").listeners.submit({ preventDefault() {} });
  analysisRoot.querySelector("#archive-file").files = [{ name: "second.zip", size: 12 }];
  analysisRoot.querySelector("#archive-file").listeners.change();
  finishAnalysis({ ok: true, json: async () => cleanResponse });
  await analysisSubmission;
  assert.equal(analysisRoot.querySelector("#analysis-results").hidden, true);
  assert.equal(analysisRoot.querySelector("#export-inspection-button").hidden, true);
  assert.match(analysisRoot.querySelector("#request-status").textContent, /File đã đổi/);

  const root = new FakeRoot();
  root.dataset.authMode = "cookie";
  root.ownerDocument.cookie = "lab_bootstrap_demo_csrf=csrf-value";
  const firstFile = { name: "first.zip", size: 12 };
  root.querySelector("#archive-file").files = [firstFile];
  let finishExport;
  const downloads = [];
  const fetchImpl = (path) => path === "/api/lab-bootstrap-demo/analyze-archive"
    ? Promise.resolve({ ok: true, json: async () => cleanResponse })
    : new Promise((resolve) => { finishExport = resolve; });
  ui.mount(root.ownerDocument, fetchImpl, async (...args) => downloads.push(args));
  await root.querySelector("#archive-form").listeners.submit({ preventDefault() {} });
  const exportSubmission = root.querySelector("#export-inspection-button").listeners.click({ preventDefault() {} });
  root.querySelector("#archive-file").files = [{ name: "second.zip", size: 12 }];
  root.querySelector("#archive-file").listeners.change();
  finishExport({
    ok: true,
    headers: { get: () => "application/zip" },
    blob: async () => "stale-report"
  });
  await exportSubmission;
  assert.deepEqual(downloads, []);
  assert.match(root.querySelector("#export-status").textContent, /File đã đổi/);
}

async function testRejectedExportShowsApiReasonWithoutDownloading() {
  const cleanResponse = JSON.parse(JSON.stringify(response));
  cleanResponse.inspection.issues = [];
  const root = new FakeRoot();
  root.querySelector("#archive-file").files = [{ name: "fixture.zip", size: 12 }];
  root.querySelector("#access-token").value = "fixture-jwt";
  const downloads = [];
  const fetchImpl = async (path) => path === "/api/lab-bootstrap-demo/analyze-archive"
    ? { ok: true, json: async () => cleanResponse }
    : {
      ok: false,
      json: async () => ({ error: "export_not_supported", message: "Inspection issues block export." })
    };
  ui.mount(root.ownerDocument, fetchImpl, async (...args) => downloads.push(args));
  await root.querySelector("#archive-form").listeners.submit({ preventDefault() {} });
  await root.querySelector("#export-inspection-button").listeners.click({ preventDefault() {} });

  assert.equal(root.querySelector("#analysis-results").hidden, false);
  assert.match(root.querySelector("#export-status").textContent, /Inspection issues block export/);
  assert.deepEqual(downloads, []);
  assert.equal(root.querySelector("#planner-status").textContent.startsWith("NOT_RUN"), true);
}

Promise.all([
  testManualJwtIsUsedWithoutPersistingIt(),
  testRunnerCookieJwtUsesCsrfWithoutExposingBearer(),
  testUnsupportedSchemaAndIncompleteTopologyErrorsAreClear(),
  testLoadingAndSuccessStatesAreReflected(),
  testReviewOnlyExportRequiresCleanInspectionAndNeverClaimsPlanning(),
  testChangingSelectedFileDoesNotReuseStaleInspectionOrExport(),
  testRejectedExportShowsApiReasonWithoutDownloading()
]).then(() => {
  process.stdout.write("Safe Demo UI assertions passed.\n");
}).catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
