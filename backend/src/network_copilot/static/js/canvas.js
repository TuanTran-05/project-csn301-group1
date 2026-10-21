// Topology canvas: layouts (pure functions) and the view/render/drag mixin the
// projects page spreads into its Alpine component.
(function () {
  "use strict";

  const NODE_W = 120;
  const NODE_H = 52;
  const BASE_W = 960;
  const BASE_H = 560;
  const GRID = 20;

  // ---------------------------------------------------------------- layouts

  const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

  function naturalCompare(a, b) {
    return collator.compare(String(a), String(b));
  }

  // Top of the picture to the bottom: the outside world, then the edge, core,
  // distribution, access and finally the hosts.
  const RANK = {
    cloud: 0, internet: 0, isp: 0,
    firewall: 1,
    core: 2, router: 2,
    distribution: 3, switch_l3: 3,
    access: 4, dmz: 4, switch: 4, segment: 4,
    management: 5, ap: 5,
    server: 6, pc: 6, host: 6,
  };

  function rankOf(item) {
    const key = item.kind in RANK ? item.kind : item.role;
    return key in RANK ? RANK[key] : 4;
  }

  function adjacency(items, edges) {
    const ids = new Set(items.map((i) => i.id));
    const map = new Map(items.map((i) => [i.id, new Set()]));
    for (const [a, b] of edges) {
      if (ids.has(a) && ids.has(b) && a !== b) {
        map.get(a).add(b);
        map.get(b).add(a);
      }
    }
    return map;
  }

  function sorted(items) {
    return [...items].sort((a, b) => naturalCompare(a.hostname, b.hostname) || a.id - b.id);
  }

  function layoutGrid(items, edges, o = {}) {
    const gx = o.gx || 170, gy = o.gy || 140;
    const list = sorted(items);
    const columns = Math.max(1, Math.ceil(Math.sqrt(list.length * 1.6)));
    const out = {};
    list.forEach((item, i) => {
      out[item.id] = { x: (i % columns) * gx, y: Math.floor(i / columns) * gy };
    });
    return out;
  }

  function layoutCircle(items, edges, o = {}) {
    const gx = o.gx || 170;
    const list = sorted(items);
    const n = list.length;
    const radius = Math.max(120, (n * gx) / (2 * Math.PI));
    const out = {};
    list.forEach((item, i) => {
      const angle = -Math.PI / 2 + (2 * Math.PI * i) / n;
      out[item.id] = { x: radius * Math.cos(angle), y: radius * Math.sin(angle) };
    });
    return out;
  }

  function layoutStar(items, edges, o = {}) {
    const gx = o.gx || 170;
    const adj = adjacency(items, edges);
    const list = sorted(items);
    let hub = list[0];
    for (const item of list) {
      if (adj.get(item.id).size > adj.get(hub.id).size) hub = item;
    }
    const rest = list.filter((i) => i.id !== hub.id);
    const radius = Math.max(170, (rest.length * gx) / (2 * Math.PI));
    const out = { [hub.id]: { x: 0, y: 0 } };
    rest.forEach((item, i) => {
      const angle = -Math.PI / 2 + (2 * Math.PI * i) / rest.length;
      out[item.id] = { x: radius * Math.cos(angle), y: radius * Math.sin(angle) };
    });
    return out;
  }

  function layoutHierarchical(items, edges, o = {}) {
    const gx = o.gx || 170, gy = o.gy || 140;
    const adj = adjacency(items, edges);
    const list = sorted(items);
    const layerOf = new Map();
    const ranks = [...new Set(list.map(rankOf))].sort((a, b) => a - b);

    if (ranks.length > 1) {
      for (const item of list) layerOf.set(item.id, ranks.indexOf(rankOf(item)));
    } else {
      // One kind of device: layer by distance from the best-connected one.
      let root = list[0];
      for (const item of list) if (adj.get(item.id).size > adj.get(root.id).size) root = item;
      const queue = [root.id];
      layerOf.set(root.id, 0);
      while (queue.length) {
        const id = queue.shift();
        for (const next of adj.get(id)) {
          if (!layerOf.has(next)) {
            layerOf.set(next, layerOf.get(id) + 1);
            queue.push(next);
          }
        }
      }
      const deepest = Math.max(0, ...layerOf.values());
      for (const item of list) if (!layerOf.has(item.id)) layerOf.set(item.id, deepest + 1);
    }

    const layers = [];
    for (const item of list) {
      const index = layerOf.get(item.id);
      (layers[index] = layers[index] || []).push(item);
    }
    const filled = layers.filter(Boolean);

    // Order each layer by the average position of its neighbours in the layer
    // beside it, a few sweeps each way, to cut crossings.
    const order = new Map();
    const reindex = () => filled.forEach((layer) => layer.forEach((item, i) => order.set(item.id, i)));
    reindex();
    const sweep = (sequence) => {
      for (const layer of sequence) {
        const keyed = layer.map((item, i) => {
          const near = [...adj.get(item.id)].filter((id) => Math.abs(layerOf.get(id) - layerOf.get(item.id)) === 1);
          const mean = near.length ? near.reduce((s, id) => s + order.get(id), 0) / near.length : i;
          return { item, mean, i };
        });
        keyed.sort((a, b) => a.mean - b.mean || a.i - b.i);
        keyed.forEach((k, i) => {
          layer[i] = k.item;
          order.set(k.item.id, i);
        });
      }
    };
    for (let pass = 0; pass < 3; pass++) {
      sweep(filled.slice(1));
      sweep([...filled].reverse().slice(1));
    }

    const out = {};
    filled.forEach((layer, row) => {
      layer.forEach((item, i) => {
        out[item.id] = { x: (i - (layer.length - 1) / 2) * gx, y: row * gy };
      });
    });
    return out;
  }

  // Fruchterman-Reingold, started from a circle so the result is repeatable.
  function layoutForce(items, edges, o = {}) {
    const gx = o.gx || 170, gy = o.gy || 140;
    const list = sorted(items);
    const n = list.length;
    if (n === 1) return { [list[0].id]: { x: 0, y: 0 } };
    const adj = adjacency(items, edges);
    const k = Math.sqrt((n * gx * gy) / n) * 0.9;
    const pos = list.map((item, i) => {
      const angle = (2 * Math.PI * i) / n;
      const radius = Math.max(100, n * 22);
      return { id: item.id, x: radius * Math.cos(angle), y: radius * Math.sin(angle), dx: 0, dy: 0 };
    });
    const index = new Map(pos.map((p, i) => [p.id, i]));
    const iterations = n > 150 ? 120 : 250;
    let temperature = k * 2;
    for (let step = 0; step < iterations; step++) {
      for (const p of pos) { p.dx = 0; p.dy = 0; }
      for (let i = 0; i < n; i++) {
        for (let j = i + 1; j < n; j++) {
          let dx = pos[i].x - pos[j].x, dy = pos[i].y - pos[j].y;
          let d = Math.hypot(dx, dy);
          if (d < 0.01) { dx = 0.01 * (i + 1); dy = 0.01 * (j + 1); d = Math.hypot(dx, dy); }
          const force = (k * k) / d;
          pos[i].dx += (dx / d) * force; pos[i].dy += (dy / d) * force;
          pos[j].dx -= (dx / d) * force; pos[j].dy -= (dy / d) * force;
        }
      }
      for (const [a, neighbours] of adj) {
        for (const b of neighbours) {
          if (a >= b) continue;
          const p = pos[index.get(a)], q = pos[index.get(b)];
          const dx = p.x - q.x, dy = p.y - q.y;
          const d = Math.max(0.01, Math.hypot(dx, dy));
          const force = (d * d) / k;
          p.dx -= (dx / d) * force; p.dy -= (dy / d) * force;
          q.dx += (dx / d) * force; q.dy += (dy / d) * force;
        }
      }
      for (const p of pos) {
        const d = Math.max(0.01, Math.hypot(p.dx, p.dy));
        const move = Math.min(d, temperature);
        p.x += (p.dx / d) * move;
        p.y += (p.dy / d) * move;
      }
      temperature *= 0.97;
    }
    const out = {};
    for (const p of pos) out[p.id] = { x: p.x, y: p.y };
    return out;
  }

  const LAYOUTS = {
    hierarchy: layoutHierarchical,
    circle: layoutCircle,
    star: layoutStar,
    force: layoutForce,
    grid: layoutGrid,
  };

  function bounds(points) {
    const xs = points.map((p) => p.x), ys = points.map((p) => p.y);
    return { minX: Math.min(...xs), maxX: Math.max(...xs), minY: Math.min(...ys), maxY: Math.max(...ys) };
  }

  // Move a layout so its centre sits on (cx, cy) and snap to the grid if asked.
  function place(layout, cx, cy, snap) {
    const values = Object.values(layout);
    if (!values.length) return layout;
    const b = bounds(values);
    const offsetX = cx - (b.minX + b.maxX) / 2, offsetY = cy - (b.minY + b.maxY) / 2;
    const out = {};
    for (const [id, p] of Object.entries(layout)) {
      let x = p.x + offsetX, y = p.y + offsetY;
      if (snap) { x = Math.round(x / GRID) * GRID; y = Math.round(y / GRID) * GRID; }
      out[id] = { x: Math.max(NODE_W / 2, x), y: Math.max(NODE_H / 2, y) };
    }
    return out;
  }

  window.NCLayout = { naturalCompare, rankOf, layout: (kind, items, edges, o) => LAYOUTS[kind](items, edges, o), place, KINDS: Object.keys(LAYOUTS), GRID };

  // ---------------------------------------------------------------- canvas mixin

  // Render bookkeeping kept outside the reactive component: writing it while a
  // getter runs would otherwise re-trigger the render.
  const cache = { slots: {}, linksByDevice: {} };

  // Pointer and wheel events can arrive several times per frame; the view and
  // the dragged nodes only need to follow once per frame. `flush` runs a
  // pending call at once (the drag end needs the final position).
  function perFrame(fn) {
    let frame = 0, args = null;
    const run = () => {
      frame = 0;
      const pending = args;
      args = null;
      if (pending) fn(...pending);
    };
    const call = (...next) => {
      args = next;
      if (!frame) frame = requestAnimationFrame(run);
    };
    call.flush = () => {
      if (frame) cancelAnimationFrame(frame);
      run();
    };
    return call;
  }

  function esc(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  window.NCCanvas = function () {
    return {
      // ---- state ----
      view: { x: 0, y: 0, w: BASE_W, h: BASE_H },
      spaceDown: false,
      panning: false,
      dragging: false,
      snap: (() => {
        try { return localStorage.getItem("nc_snap") === "1"; } catch { return false; }
      })(),
      menu: null,
      helpOpen: false,
      layoutMenuOpen: false,
      _fitted: false,

      initCanvas() {
        const typing = (e) => ["INPUT", "TEXTAREA", "SELECT"].includes((e.target && e.target.tagName) || "");
        window.addEventListener("keydown", (e) => {
          if (e.code === "Space" && !typing(e) && this.tab === "topology") {
            this.spaceDown = true;
            e.preventDefault();
          }
        });
        window.addEventListener("keyup", (e) => {
          if (e.code === "Space") this.spaceDown = false;
        });
        window.addEventListener("click", () => { this.menu = null; });
        this._previewMove = perFrame((e) => this._linkPreviewAt(this.linkSourceId, e));
        this.$watch("linkSourceId", (id) => { if (!id) this._hideLinkPreview(); });
        this.$watch("linkMode", (on) => { if (!on) this._hideLinkPreview(); });
      },

      // x-html re-parses its markup on every reactive change. The canvas
      // layers are only repainted when their markup really changed, so a
      // reload that brings the same diagram costs nothing.
      paintSvg(el, markup) {
        if (el._ncMarkup === markup) return;
        el._ncMarkup = markup;
        el.innerHTML = markup;
      },

      // ---- geometry ----
      get viewBoxAttr() {
        return `${this.view.x} ${this.view.y} ${this.view.w} ${this.view.h}`;
      },

      _aspect() {
        const el = this.$refs.canvas;
        return el && el.clientWidth && el.clientHeight ? el.clientWidth / el.clientHeight : BASE_W / BASE_H;
      },

      get zoomPercent() {
        const el = this.$refs.canvas;
        return Math.round((100 * ((el && el.clientWidth) || BASE_W)) / this.view.w);
      },

      _svgPoint(event) {
        const svg = this.$refs.canvas;
        const point = svg.createSVGPoint();
        point.x = event.clientX;
        point.y = event.clientY;
        const mapped = point.matrixTransform(svg.getScreenCTM().inverse());
        return { x: mapped.x, y: mapped.y };
      },

      contentBox() {
        if (!this.devices.length) return { x: 0, y: 0, w: BASE_W, h: BASE_H };
        const points = this.devices.map((d) => this.nodePos(d));
        const b = bounds(points);
        return { x: b.minX - NODE_W / 2, y: b.minY - NODE_H / 2, w: b.maxX - b.minX + NODE_W, h: b.maxY - b.minY + NODE_H };
      },

      // ---- zoom / pan / fit ----
      setView(centerX, centerY, w) {
        const width = Math.min(20000, Math.max(160, w));
        const height = width / this._aspect();
        this.view = { x: centerX - width / 2, y: centerY - height / 2, w: width, h: height };
      },

      fitView(nodesOnly) {
        const box = this.contentBox();
        const margin = 70;
        const aspect = this._aspect();
        const width = Math.max(box.w + margin * 2, (box.h + margin * 2) * aspect);
        // Never zoom in past 100%: one or two devices would otherwise fill the canvas.
        const el = this.$refs.canvas;
        const natural = (el && el.clientWidth) || BASE_W;
        this.setView(box.x + box.w / 2, box.y + box.h / 2, nodesOnly === false ? BASE_W : Math.max(width, natural));
      },

      resetView() {
        this.setView(BASE_W / 2, BASE_H / 2, BASE_W);
      },

      zoomBy(factor, anchor) {
        const v = this.view;
        const point = anchor || { x: v.x + v.w / 2, y: v.y + v.h / 2 };
        const width = Math.min(20000, Math.max(160, v.w * factor));
        const scale = width / v.w;
        const height = width / this._aspect();
        this.view = {
          x: point.x - (point.x - v.x) * scale,
          y: point.y - (point.y - v.y) * (height / v.h),
          w: width,
          h: height,
        };
      },

      // Wheel ticks of one frame are folded into a single zoom step.
      onWheel(event) {
        const pending = this._wheelZoom;
        const factor = (pending ? pending.factor : 1) * Math.pow(1.0015, event.deltaY);
        this._wheelZoom = { factor, anchor: this._svgPoint(event) };
        if (pending) return;
        requestAnimationFrame(() => {
          const step = this._wheelZoom;
          this._wheelZoom = null;
          if (step) this.zoomBy(step.factor, step.anchor);
        });
      },

      // Keep a point (a device just added) on screen without changing the zoom.
      // The view only moves as far as needed, so the diagram does not jump.
      _reveal(p) {
        const v = this.view;
        const margin = 20;
        const shift = (low, high, start, size) =>
          low < start ? low - start : high > start + size ? high - start - size : 0;
        const dx = shift(p.x - NODE_W / 2 - margin, p.x + NODE_W / 2 + margin, v.x, v.w);
        const dy = shift(p.y - NODE_H / 2 - margin, p.y + NODE_H / 2 + margin, v.y, v.h);
        if (dx || dy) this.view = { ...v, x: v.x + dx, y: v.y + dy };
      },

      _startPan(event) {
        const svg = this.$refs.canvas;
        const scale = svg.getScreenCTM().a || 1;
        const startX = event.clientX, startY = event.clientY;
        const origin = { ...this.view };
        this.panning = true;
        const move = perFrame((e) => {
          this.view = {
            ...origin,
            x: origin.x - (e.clientX - startX) / scale,
            y: origin.y - (e.clientY - startY) / scale,
          };
        });
        const up = () => {
          move.flush();
          window.removeEventListener("pointermove", move);
          window.removeEventListener("pointerup", up);
          this.panning = false;
        };
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", up);
        event.preventDefault();
      },

      // ---- minimap ----
      get minimapBox() {
        const box = this.contentBox();
        const v = this.view;
        const x = Math.min(box.x, v.x), y = Math.min(box.y, v.y);
        const right = Math.max(box.x + box.w, v.x + v.w), bottom = Math.max(box.y + box.h, v.y + v.h);
        return { x: x - 40, y: y - 40, w: right - x + 80, h: bottom - y + 80 };
      },

      // Nodes and cables only: the view rectangle is bound separately in the
      // page, so zooming and panning do not rebuild the whole minimap.
      get minimapSvg() {
        const positions = {};
        for (const device of this.devices) positions[device.id] = this.nodePos(device);
        const parts = [];
        for (const link of this.links) {
          const p = positions[link.device_a_id], q = positions[link.device_b_id];
          if (!p || !q) continue;
          parts.push(`<line x1="${p.x}" y1="${p.y}" x2="${q.x}" y2="${q.y}" class="mm-link" />`);
        }
        for (const device of this.devices) {
          const p = positions[device.id];
          const on = this.multi.includes(device.id) || device.id === this.selectedNodeId;
          parts.push(`<rect x="${p.x - 28}" y="${p.y - 14}" width="56" height="28" rx="6" class="mm-node${on ? " on" : ""}" />`);
        }
        return parts.join("");
      },

      onMinimapPointer(event) {
        const el = event.currentTarget;
        const move = perFrame((e) => {
          const point = el.createSVGPoint();
          point.x = e.clientX;
          point.y = e.clientY;
          const p = point.matrixTransform(el.getScreenCTM().inverse());
          this.setView(p.x, p.y, this.view.w);
        });
        move(event);
        move.flush();
        const up = () => {
          move.flush();
          window.removeEventListener("pointermove", move);
          window.removeEventListener("pointerup", up);
        };
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", up);
      },

      // ---- rendering ----
      _slotsFor(positions) {
        const seen = {};
        const slots = {};
        const byDevice = {};
        for (const link of this.links) {
          if (!positions[link.device_a_id] || !positions[link.device_b_id]) continue;
          const key = [link.device_a_id, link.device_b_id].sort().join("-");
          slots[link.id] = (seen[key] = (seen[key] || 0) + 1) - 1;
          (byDevice[link.device_a_id] = byDevice[link.device_a_id] || []).push(link);
          (byDevice[link.device_b_id] = byDevice[link.device_b_id] || []).push(link);
        }
        cache.slots = slots;
        cache.linksByDevice = byDevice;
      },

      _linkOpen(link) {
        const selected = link.id === this.selectedLinkId;
        const drift = this.driftOf ? this.driftOf(link.id) : "";
        return `<g class="topo-link${selected ? " selected" : ""}" data-link-id="${link.id}"${drift ? ` data-drift="${drift}"` : ""}>`;
      },

      _linkInner(link, a, b, slot) {
        const dx = b.x - a.x, dy = b.y - a.y;
        const length = Math.hypot(dx, dy) || 1;
        const nx = (-dy / length) * slot * 14, ny = (dx / length) * slot * 14;
        const x1 = a.x + nx, y1 = a.y + ny, x2 = b.x + nx, y2 = b.y + ny;
        const color = window.NCShared.LINK_COLORS[link.link_type] || "#75899f";
        const selected = link.id === this.selectedLinkId;
        const dash = link.link_type === "trunk" ? ' stroke-dasharray="7 4"' : "";
        const bundle = link.channel_id ? 5 : 0;
        return (
          `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="transparent" stroke-width="16" />` +
          `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${color}" stroke-width="${selected ? 4 : 2 + bundle / 2}"${dash} />` +
          this._linkLabels(link, x1, y1, x2, y2, color)
        );
      },

      _nodeMarkup(device, p, selected) {
        const status = ["online", "offline"].includes(device.status) ? device.status : "unknown";
        const planned = device.lifecycle === "planned" ? " planned" : "";
        const unmanaged = device.managed === false ? " unmanaged" : "";
        const sub = this.nodeSubtitle ? this.nodeSubtitle(device) : esc(device.role);
        return (
          `<g class="topo-node${selected ? " selected" : ""}${planned}${unmanaged} env-${esc(device.environment)}" data-node-id="${device.id}" transform="translate(${p.x - NODE_W / 2},${p.y - NODE_H / 2})">` +
          `<rect width="${NODE_W}" height="${NODE_H}" rx="8" />` +
          `<circle class="dot ${status}" cx="${NODE_W - 10}" cy="10" r="4.5" />` +
          `<g class="icon" transform="translate(6,${NODE_H / 2 - 14}) scale(1.15)">${window.NCShared.ICONS[window.NCShared.templateKeyFor(device)] || ""}</g>` +
          `<text class="host" x="${NODE_W / 2 + 10}" y="24" text-anchor="middle">${esc(device.hostname)}</text>` +
          `<text class="sub" x="${NODE_W / 2 + 10}" y="41" text-anchor="middle">${sub}</text>` +
          `</g>`
        );
      },

      // Links first, nodes on top. Group boxes and notes are drawn by the page
      // below them (see `decorationsSvg`).
      get topologySvg() {
        const positions = {};
        for (const device of this.devices) positions[device.id] = this.nodePos(device);
        this._slotsFor(positions);
        const parts = [];
        for (const link of this.links) {
          const a = positions[link.device_a_id], b = positions[link.device_b_id];
          if (!a || !b) continue;
          parts.push(this._linkOpen(link) + this._linkInner(link, a, b, cache.slots[link.id]) + "</g>");
        }
        for (const device of this.devices) {
          const selected =
            device.id === this.selectedNodeId ||
            device.id === this.linkSourceId ||
            this.multi.includes(device.id);
          parts.push(this._nodeMarkup(device, positions[device.id], selected));
        }
        return parts.join("");
      },

      // While dragging, only the moved nodes and their cables are touched in the
      // DOM; the component state is written once, when the drag ends.
      _patchDrag(current) {
        const svg = this.$refs.canvas;
        if (!svg) return;
        const touched = new Set();
        for (const [id, p] of Object.entries(current)) {
          const el = svg.querySelector(`[data-node-id="${id}"]`);
          if (el) el.setAttribute("transform", `translate(${p.x - NODE_W / 2},${p.y - NODE_H / 2})`);
          for (const link of cache.linksByDevice[id] || []) touched.add(link);
        }
        const at = (id) => current[id] || this.nodePos(this.deviceById(id));
        for (const link of touched) {
          const g = svg.querySelector(`[data-link-id="${link.id}"]`);
          if (g) g.innerHTML = this._linkInner(link, at(link.device_a_id), at(link.device_b_id), cache.slots[link.id] || 0);
        }
      },

      _snapped(x, y) {
        if (!this.snap) return { x, y };
        return { x: Math.round(x / GRID) * GRID, y: Math.round(y / GRID) * GRID };
      },

      // The nearest place around `p` where a new device does not cover another
      // one, so drops and clicks on the palette never stack devices.
      _freeSpot(p) {
        const taken = this.devices.map((d) => this.nodePos(d));
        const free = (x, y) => taken.every((q) => Math.abs(q.x - x) >= NODE_W + 10 || Math.abs(q.y - y) >= NODE_H + 10);
        const stepX = NODE_W + 30, stepY = NODE_H + 40;
        const at = (i, j) => {
          const s = this._snapped(p.x + i * stepX, p.y + j * stepY);
          return { x: Math.round(s.x), y: Math.round(s.y) };
        };
        for (let ring = 0; ring < 12; ring++) {
          for (let j = -ring; j <= ring; j++) {
            for (let i = -ring; i <= ring; i++) {
              if (Math.max(Math.abs(i), Math.abs(j)) !== ring) continue;
              const c = at(i, j);
              if (free(c.x, c.y)) return c;
            }
          }
        }
        return at(0, 0);
      },

      toggleSnap() {
        this.snap = !this.snap;
        try { localStorage.setItem("nc_snap", this.snap ? "1" : "0"); } catch { /* private mode */ }
      },

      // ---- cabling preview ----
      // A dashed cable follows the pointer from the source device while
      // cabling; the device under the pointer is highlighted as the target.
      _linkPreviewAt(sourceId, event) {
        const line = this.$refs.linkPreview;
        const source = sourceId && this.deviceById(sourceId);
        if (!line || !source || !this.linkMode) return;
        const from = this.nodePos(source);
        const to = this._svgPoint(event);
        line.setAttribute("x1", from.x);
        line.setAttribute("y1", from.y);
        line.setAttribute("x2", to.x);
        line.setAttribute("y2", to.y);
        line.style.display = "";
        const target = this._nodeElementAt(event);
        const svg = this.$refs.canvas;
        for (const el of svg.querySelectorAll(".topo-node.link-target")) {
          if (el !== target) el.classList.remove("link-target");
        }
        if (target && Number(target.dataset.nodeId) !== Number(sourceId)) target.classList.add("link-target");
      },

      _hideLinkPreview() {
        const line = this.$refs.linkPreview;
        if (line) line.style.display = "none";
        const svg = this.$refs.canvas;
        if (svg) for (const el of svg.querySelectorAll(".topo-node.link-target")) el.classList.remove("link-target");
      },

      _nodeElementAt(event) {
        const hit = document.elementFromPoint(event.clientX, event.clientY);
        return hit && hit.closest ? hit.closest(".topology-canvas [data-node-id]") : null;
      },

      onCanvasPointerMove(event) {
        if (this._drag || !this.linkMode || !this.linkSourceId) return;
        this._previewMove(event);
      },

      // ---- pointer handling ----
      onCanvasPointerDown(event) {
        this.menu = null;
        if (event.button === 1 || (event.button === 0 && this.spaceDown)) {
          this._startPan(event);
          return;
        }
        if (event.button !== 0) return;
        const nodeEl = event.target.closest("[data-node-id]");
        const linkEl = event.target.closest("[data-link-id]");
        const extra = event.target.closest("[data-deco-id]");
        if (nodeEl) {
          const id = Number(nodeEl.dataset.nodeId);
          const device = this.deviceById(id);
          if (!device) return;
          if ((event.shiftKey || event.ctrlKey || event.metaKey) && !this.linkMode) {
            this.multi = this.multi.includes(id)
              ? this.multi.filter((other) => other !== id)
              : [...this.multi, id];
            this.selectedNodeId = this.multi.length ? this.multi[this.multi.length - 1] : null;
            this.selectedLinkId = null;
            event.preventDefault();
            return;
          }
          if (!this.multi.includes(id)) this.multi = [id];
          const start = this._svgPoint(event);
          const moving = this.canEdit && !this.linkMode ? this.multi : [id];
          const origins = {};
          for (const other of moving) {
            const target = this.deviceById(other);
            if (target) origins[other] = { ...this.nodePos(target) };
          }
          this._drag = { id, origins, current: { ...origins }, startPoint: start, startX: event.clientX, startY: event.clientY, moved: false, linking: false };
          const move = perFrame((e) => this._onDragMove(e));
          const up = (e) => {
            window.removeEventListener("pointermove", move);
            window.removeEventListener("pointerup", up);
            move.flush();
            this._onDragEnd(e);
          };
          window.addEventListener("pointermove", move);
          window.addEventListener("pointerup", up);
          event.preventDefault();
        } else if (extra && this.onDecoPointerDown) {
          this.onDecoPointerDown(event, extra);
        } else if (linkEl) {
          this.multi = [];
          this.selectLink(Number(linkEl.dataset.linkId));
        } else {
          this._startBand(event);
        }
      },

      _startBand(event) {
        const additive = event.shiftKey || event.ctrlKey || event.metaKey;
        const start = this._svgPoint(event);
        if (!additive) {
          this.multi = [];
          this.selectedNodeId = null;
          this.selectedLinkId = null;
        }
        if (this.linkMode) return;
        const base = additive ? [...this.multi] : [];
        const rect = this.$refs.bandRect;
        let last = base.join(",");
        const draw = (x1, y1, x2, y2) => {
          rect.setAttribute("x", Math.min(x1, x2));
          rect.setAttribute("y", Math.min(y1, y2));
          rect.setAttribute("width", Math.abs(x2 - x1));
          rect.setAttribute("height", Math.abs(y2 - y1));
          rect.style.display = "block";
        };
        const move = perFrame((e) => {
          const p = this._svgPoint(e);
          draw(start.x, start.y, p.x, p.y);
          const left = Math.min(start.x, p.x), right = Math.max(start.x, p.x);
          const top = Math.min(start.y, p.y), bottom = Math.max(start.y, p.y);
          const inside = this.devices
            .filter((d) => {
              const q = this.nodePos(d);
              return q.x >= left && q.x <= right && q.y >= top && q.y <= bottom;
            })
            .map((d) => d.id);
          const next = [...new Set([...base, ...inside])];
          if (next.join(",") !== last) {
            last = next.join(",");
            this.multi = next;
            this.selectedNodeId = next.length === 1 ? next[0] : null;
          }
        });
        const up = () => {
          window.removeEventListener("pointermove", move);
          window.removeEventListener("pointerup", up);
          move.flush();
          rect.style.display = "none";
        };
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", up);
        event.preventDefault();
      },

      _onDragMove(event) {
        const drag = this._drag;
        if (!drag) return;
        if (!drag.moved && !drag.linking && Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < 4) return;
        if (!this.canEdit) return;
        if (this.linkMode) {
          // Cabling by drag: pull a cable from this device onto another one.
          drag.linking = true;
          this._linkPreviewAt(drag.id, event);
          return;
        }
        if (!drag.moved) this.dragging = true;
        drag.moved = true;
        this._dragTo(drag, this._svgPoint(event));
        this._edgeScroll(drag, event);
      },

      // Devices follow the pointer anywhere on the (unbounded) canvas.
      _dragTo(drag, point) {
        drag.lastPoint = point;
        const dx = point.x - drag.startPoint.x;
        const dy = point.y - drag.startPoint.y;
        const current = {};
        for (const [id, origin] of Object.entries(drag.origins)) {
          const snapped = this._snapped(origin.x + dx, origin.y + dy);
          current[id] = { x: Math.round(snapped.x), y: Math.round(snapped.y) };
        }
        drag.current = current;
        this._patchDrag(current);
      },

      // Holding a dragged device near the edge of the canvas scrolls the view,
      // so a device is never pulled out of sight.
      _edgeScroll(drag, event) {
        const rect = this.$refs.canvas.getBoundingClientRect();
        const margin = 36;
        const side = (pos, low, high) => (pos < low + margin ? -1 : pos > high - margin ? 1 : 0);
        drag.edge = { x: side(event.clientX, rect.left, rect.right), y: side(event.clientY, rect.top, rect.bottom) };
        if (drag.edgeFrame || (!drag.edge.x && !drag.edge.y)) return;
        const step = () => {
          if (this._drag !== drag || (!drag.edge.x && !drag.edge.y)) {
            drag.edgeFrame = 0;
            return;
          }
          const speed = this.view.w / 90;
          const sx = drag.edge.x * speed, sy = drag.edge.y * speed;
          this.view = { ...this.view, x: this.view.x + sx, y: this.view.y + sy };
          this._dragTo(drag, { x: drag.lastPoint.x + sx, y: drag.lastPoint.y + sy });
          drag.edgeFrame = requestAnimationFrame(step);
        };
        drag.edgeFrame = requestAnimationFrame(step);
      },

      async _onDragEnd(event) {
        const drag = this._drag;
        this._drag = null;
        this.dragging = false;
        if (!drag) return;
        if (drag.edgeFrame) cancelAnimationFrame(drag.edgeFrame);
        const device = this.deviceById(drag.id);
        if (!device) return;
        if (drag.linking) {
          const targetEl = event && this._nodeElementAt(event);
          const target = targetEl && this.deviceById(targetEl.dataset.nodeId);
          this._hideLinkPreview();
          this.linkSourceId = null;
          if (target && target.id !== device.id) await this.quickLink(device.id, target.id);
          return;
        }
        if (!drag.moved) {
          this.clickNode(device);
          return;
        }
        const before = drag.origins;
        const after = drag.current;
        for (const [id, p] of Object.entries(after)) {
          const moved = this.deviceById(id);
          if (moved) { moved.pos_x = p.x; moved.pos_y = p.y; }
        }
        await this._savePositions(before, after, "Di chuyển");
      },

      // ---- automatic layout ----
      async applyLayout(kind) {
        this.layoutMenuOpen = false;
        if (!this.canEdit || !this.devices.length) return;
        const selected = this.multi.map((id) => this.deviceById(id)).filter(Boolean);
        const targets = selected.length >= 2 ? selected : this.devices;
        const items = targets.map((d) => ({ id: d.id, hostname: d.hostname, role: d.role, kind: d.kind || window.NCShared.templateKeyFor(d) }));
        const edges = this.links.map((l) => [l.device_a_id, l.device_b_id]);
        const raw = window.NCLayout.layout(kind, items, edges);
        const before = {};
        for (const d of targets) before[d.id] = { ...this.nodePos(d) };
        const current = Object.values(before);
        const b = bounds(current);
        const keepCenter = selected.length >= 2;
        const cx = keepCenter ? (b.minX + b.maxX) / 2 : 120 + (Math.max(...Object.values(raw).map((p) => p.x)) - Math.min(...Object.values(raw).map((p) => p.x))) / 2;
        const cy = keepCenter ? (b.minY + b.maxY) / 2 : 100 + (Math.max(...Object.values(raw).map((p) => p.y)) - Math.min(...Object.values(raw).map((p) => p.y))) / 2;
        const after = window.NCLayout.place(raw, cx, cy, this.snap);
        for (const [id, p] of Object.entries(after)) {
          const d = this.deviceById(id);
          d.pos_x = p.x;
          d.pos_y = p.y;
        }
        await this._savePositions(before, after, "Bố trí tự động");
        this.$nextTick(() => this.fitView());
      },

      // ---- keyboard (everything except Delete / undo, handled by the page) ----
      onCanvasKey(event) {
        const key = event.key;
        const mod = event.ctrlKey || event.metaKey;
        if (key === "?" || (key === "/" && event.shiftKey)) {
          this.helpOpen = !this.helpOpen;
          return true;
        }
        if (key === "Escape" && this.helpOpen) {
          this.helpOpen = false;
          return true;
        }
        if (!mod && (key === "f" || key === "F")) { this.fitView(); return true; }
        if (!mod && (key === "+" || key === "=")) { this.zoomBy(1 / 1.25); return true; }
        if (!mod && (key === "-" || key === "_")) { this.zoomBy(1.25); return true; }
        if (!mod && key === "0") { this.resetView(); return true; }
        if (!mod && (key === "g" || key === "G")) { this.toggleSnap(); return true; }
        if (mod && (key === "c" || key === "C")) { this.copySelection(); return true; }
        if (mod && (key === "v" || key === "V")) { this.pasteClipboard(); return true; }
        if (mod && (key === "d" || key === "D")) { event.preventDefault(); this.duplicateSelection(); return true; }
        const arrows = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
        if (arrows[key] && this.multi.length && this.canEdit) {
          event.preventDefault();
          this.nudge(arrows[key][0] * (event.shiftKey ? GRID * 2 : GRID / 2), arrows[key][1] * (event.shiftKey ? GRID * 2 : GRID / 2));
          return true;
        }
        return false;
      },

      async nudge(dx, dy) {
        const before = {}, after = {};
        for (const id of this.multi) {
          const d = this.deviceById(id);
          if (!d) continue;
          const p = this.nodePos(d);
          before[id] = { ...p };
          after[id] = { x: p.x + dx, y: p.y + dy };
          d.pos_x = after[id].x;
          d.pos_y = after[id].y;
        }
        clearTimeout(this._nudgeTimer);
        this._nudgeBefore = this._nudgeBefore || before;
        this._nudgeTimer = setTimeout(async () => {
          const original = this._nudgeBefore;
          this._nudgeBefore = null;
          const final = {};
          for (const id of Object.keys(original)) {
            const d = this.deviceById(id);
            if (d) final[id] = { x: d.pos_x, y: d.pos_y };
          }
          await this._savePositions(original, final, "Dịch chuyển");
        }, 400);
      },
    };
  };
})();
