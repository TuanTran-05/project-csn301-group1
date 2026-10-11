const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.resolve(__dirname, "../../src/network_copilot/static/js/canvas.js"), "utf8");
const sandbox = { window: {}, localStorage: { getItem: () => null, setItem() {} }, Intl, Math, console };
vm.runInNewContext(source, sandbox, { filename: "canvas.js" });
const { NCLayout, NCCanvas } = sandbox.window;

const plain = (value) => JSON.parse(JSON.stringify(value)); // objects from the vm have another realm
const dev = (id, hostname, role) => ({ id, hostname, role });
const centred = (layout) => Object.values(layout);

function naturalSort() {
  const names = ["N10", "N2", "N1", "N100", "R1", "N20"].sort(NCLayout.naturalCompare);
  assert.deepEqual(names, ["N1", "N2", "N10", "N20", "N100", "R1"]);
}

function everyLayoutPlacesEveryNodeOnce() {
  const items = [dev(1, "R1", "core"), dev(2, "SW1", "distribution"), dev(3, "SW2", "access"), dev(4, "SW3", "access"), dev(5, "FW1", "firewall")];
  const edges = [[1, 2], [2, 3], [2, 4], [5, 1]];
  for (const kind of NCLayout.KINDS) {
    const out = NCLayout.layout(kind, items, edges);
    assert.deepEqual(Object.keys(out).map(Number).sort(), [1, 2, 3, 4, 5], kind);
    for (const p of centred(out)) assert.ok(Number.isFinite(p.x) && Number.isFinite(p.y), kind);
    const unique = new Set(centred(out).map((p) => `${Math.round(p.x)},${Math.round(p.y)}`));
    assert.equal(unique.size, 5, `${kind} must not stack nodes`);
  }
}

function hierarchyPutsRolesInOrder() {
  const items = [dev(1, "A1", "access"), dev(2, "C1", "core"), dev(3, "D1", "distribution"), dev(4, "F1", "firewall"), dev(5, "I1", "isp")];
  const out = NCLayout.layout("hierarchy", items, [[5, 4], [4, 2], [2, 3], [3, 1]]);
  const y = (id) => out[id].y;
  assert.ok(y(5) < y(4) && y(4) < y(2) && y(2) < y(3) && y(3) < y(1), JSON.stringify(out));
}

function hierarchyFallsBackToDistanceForOneKindOfDevice() {
  const items = [1, 2, 3, 4].map((i) => dev(i, `S${i}`, "access"));
  const out = NCLayout.layout("hierarchy", items, [[1, 2], [1, 3], [3, 4]]);
  assert.equal(out[1].y, 0);
  assert.ok(out[2].y === 140 && out[3].y === 140 && out[4].y === 280);
}

function starPutsTheBestConnectedNodeInTheMiddle() {
  const items = [1, 2, 3, 4].map((i) => dev(i, `S${i}`, "access"));
  const out = NCLayout.layout("star", items, [[3, 1], [3, 2], [3, 4]]);
  assert.deepEqual(plain(out[3]), { x: 0, y: 0 });
}

function forceIsDeterministicAndKeepsNeighboursCloser() {
  const items = Array.from({ length: 12 }, (_, i) => dev(i + 1, `N${i + 1}`, "access"));
  const edges = items.slice(1).map((it, i) => [i + 1, i + 2]); // a chain
  const a = NCLayout.layout("force", items, edges);
  const b = NCLayout.layout("force", items, edges);
  assert.deepEqual(plain(a), plain(b));
  const d = (x, y) => Math.hypot(a[x].x - a[y].x, a[x].y - a[y].y);
  assert.ok(d(1, 2) < d(1, 12), "chain ends should be farther apart than neighbours");
}

function layoutsHandleOneAndZeroEdges() {
  for (const kind of NCLayout.KINDS) {
    const one = NCLayout.layout(kind, [dev(1, "R1", "core")], []);
    assert.ok(one[1] && Number.isFinite(one[1].x), kind);
  }
}

function placeCentresAndSnaps() {
  const placed = NCLayout.place({ 1: { x: -50, y: -50 }, 2: { x: 50, y: 50 } }, 500, 300, true);
  assert.deepEqual(plain(placed), { 1: { x: 460, y: 260 }, 2: { x: 560, y: 360 } });
  const kept = NCLayout.place({ 1: { x: -500, y: -500 } }, 0, 0, false);
  assert.ok(kept[1].x >= 60 && kept[1].y >= 26, "nodes stay inside the canvas");
}

function zoomKeepsThePointUnderTheCursor() {
  const canvas = NCCanvas();
  canvas.$refs = { canvas: { clientWidth: 960, clientHeight: 560 } };
  const anchor = { x: 300, y: 200 };
  const before = { ...canvas.view };
  canvas.zoomBy(0.5, anchor);
  const after = canvas.view;
  assert.equal(after.w, before.w * 0.5);
  // The anchor stays at the same fraction of the viewport.
  assert.ok(Math.abs((anchor.x - after.x) / after.w - (anchor.x - before.x) / before.w) < 1e-9);
  assert.ok(Math.abs((anchor.y - after.y) / after.h - (anchor.y - before.y) / before.h) < 1e-9);
  canvas.zoomBy(1e-6);
  assert.ok(canvas.view.w >= 160, "zoom is clamped");
  canvas.zoomBy(1e9);
  assert.ok(canvas.view.w <= 20000, "zoom is clamped");
}

function fitContainsEveryNode() {
  const canvas = NCCanvas();
  canvas.$refs = { canvas: { clientWidth: 1000, clientHeight: 500 } };
  canvas.devices = [{ id: 1, pos_x: 100, pos_y: 100 }, { id: 2, pos_x: 2000, pos_y: 900 }];
  canvas.nodePos = (d) => ({ x: d.pos_x, y: d.pos_y });
  canvas.fitView();
  const v = canvas.view;
  for (const d of canvas.devices) {
    assert.ok(d.pos_x - 60 >= v.x && d.pos_x + 60 <= v.x + v.w, "x inside");
    assert.ok(d.pos_y - 26 >= v.y && d.pos_y + 26 <= v.y + v.h, "y inside");
  }
  assert.ok(Math.abs(v.w / v.h - 2) < 1e-9, "view keeps the element's aspect");
}

const cases = {
  natural_sort: naturalSort,
  every_layout_places_every_node: everyLayoutPlacesEveryNodeOnce,
  hierarchy_orders_roles: hierarchyPutsRolesInOrder,
  hierarchy_distance_fallback: hierarchyFallsBackToDistanceForOneKindOfDevice,
  star_hub: starPutsTheBestConnectedNodeInTheMiddle,
  force_is_deterministic: forceIsDeterministicAndKeepsNeighboursCloser,
  layouts_tiny_inputs: layoutsHandleOneAndZeroEdges,
  place_centres_and_snaps: placeCentresAndSnaps,
  zoom_anchor: zoomKeepsThePointUnderTheCursor,
  fit_contains_nodes: fitContainsEveryNode,
};

const name = process.argv[2];
assert.ok(cases[name], `Unknown case ${name}`);
cases[name]();
process.stdout.write(JSON.stringify({ case: name, ok: true }));
