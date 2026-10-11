// Design aids for the topology page: blueprint gallery, design checks (lint),
// addressing overview and notes / group boxes drawn on the canvas.
// Merged into the page component with the same mix() as the canvas module.
(function () {
  const esc = (value) => window.NCShared.esc(value);
  const NOTE_COLORS = ["#fde68a", "#bbf7d0", "#bfdbfe", "#fbcfe8", "#e5e7eb"];
  const GROUP_COLORS = ["#3b82f6", "#22c55e", "#f59e0b", "#a855f7", "#64748b"];

  function wrap(text, width) {
    const lines = [];
    for (const paragraph of String(text || "").split("\n")) {
      let line = "";
      for (const word of paragraph.split(/\s+/)) {
        if ((line + " " + word).trim().length > width && line) {
          lines.push(line);
          line = word;
        } else {
          line = (line + " " + word).trim();
        }
      }
      lines.push(line);
    }
    return lines;
  }

  window.NCDesignAids = function () {
    return {
      notes: [],
      selectedDecoId: null,
      placePlanned: false,
      blueprints: [],
      blueprintOpen: false,
      blueprintKey: null,
      blueprintParams: {},
      blueprintPlanned: false,
      blueprintBusy: false,
      blueprintError: "",
      lintOpen: false,
      lintResult: null,
      lintError: "",
      ipamInfo: null,

      resetDesignAids() {
        this.notes = [];
        this.selectedDecoId = null;
        this.blueprints = [];
        this.blueprintOpen = false;
        this.lintOpen = false;
        this.lintResult = null;
        this.ipamInfo = null;
      },

      // ---- node text -------------------------------------------------------
      nodeSubtitle(device) {
        if (device.managed === false) return esc(device.data_ip || device.kind || device.role);
        if (device.lifecycle === "planned") return "kế hoạch · " + esc(device.role);
        return esc(device.management_ip || device.role);
      },

      get selectedDeco() {
        return this.notes.find((n) => n.id === this.selectedDecoId) || null;
      },

      // ---- canvas decorations ---------------------------------------------
      decorationMarkup() {
        const groups = [], notes = [];
        for (const n of this.notes) {
          const selected = n.id === this.selectedDecoId ? " selected" : "";
          if (n.kind === "group") {
            const c = esc(n.color || GROUP_COLORS[0]);
            groups.push(
              `<g class="deco group${selected}" data-deco-id="${n.id}">` +
                `<rect class="gbody" x="${n.x}" y="${n.y}" width="${n.width}" height="${n.height}" rx="10" fill="${c}" fill-opacity="0.08" stroke="${c}" />` +
                `<rect class="ghead" data-deco-move="1" x="${n.x}" y="${n.y}" width="${n.width}" height="22" rx="10" fill="${c}" fill-opacity="0.22" />` +
                `<text class="glabel" x="${n.x + 10}" y="${n.y + 15}">${esc(n.text.split("\n")[0].slice(0, 60))}</text>` +
                `<rect class="handle" data-deco-resize="1" x="${n.x + n.width - 12}" y="${n.y + n.height - 12}" width="12" height="12" fill="${c}" />` +
                `</g>`
            );
          } else {
            const c = esc(n.color || NOTE_COLORS[0]);
            const lines = wrap(n.text, Math.max(8, Math.floor(n.width / 7))).slice(0, Math.max(1, Math.floor((n.height - 12) / 15)));
            notes.push(
              `<g class="deco note${selected}" data-deco-id="${n.id}" data-deco-move="1">` +
                `<rect x="${n.x}" y="${n.y}" width="${n.width}" height="${n.height}" rx="4" fill="${c}" />` +
                lines.map((line, i) => `<text x="${n.x + 8}" y="${n.y + 20 + i * 15}">${esc(line)}</text>`).join("") +
                `<rect class="handle" data-deco-resize="1" x="${n.x + n.width - 10}" y="${n.y + n.height - 10}" width="10" height="10" />` +
                `</g>`
            );
          }
        }
        // Groups sit under the devices, notes above the groups.
        return groups.join("") + notes.join("");
      },

      onDecoPointerDown(event, el) {
        const id = Number(el.dataset.decoId);
        const note = this.notes.find((n) => n.id === id);
        if (!note) return;
        this.selectedDecoId = id;
        this.multi = [];
        this.selectedNodeId = null;
        this.selectedLinkId = null;
        const resize = !!event.target.closest("[data-deco-resize]");
        const move = !!event.target.closest("[data-deco-move]");
        if (!this.canEdit || (!resize && !move)) return;
        const start = this._svgPoint(event);
        const origin = { x: note.x, y: note.y, width: note.width, height: note.height };
        let changed = false;
        const onMove = (e) => {
          const p = this._svgPoint(e);
          const dx = p.x - start.x, dy = p.y - start.y;
          if (!changed && Math.hypot(dx, dy) < 3) return;
          changed = true;
          if (resize) {
            note.width = Math.max(40, Math.round(origin.width + dx));
            note.height = Math.max(30, Math.round(origin.height + dy));
          } else {
            const s = this._snapped ? this._snapped(origin.x + dx, origin.y + dy) : { x: origin.x + dx, y: origin.y + dy };
            note.x = Math.round(s.x);
            note.y = Math.round(s.y);
          }
        };
        const onUp = () => {
          window.removeEventListener("pointermove", onMove);
          window.removeEventListener("pointerup", onUp);
          if (changed) this.saveNote(note);
        };
        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", onUp);
        event.preventDefault();
      },

      // ---- notes -----------------------------------------------------------
      async addNote(kind) {
        if (!this.canEdit) return;
        const count = this.notes.length % 8;
        const cx = Math.round(this.view.x + this.view.w / 2), cy = Math.round(this.view.y + this.view.h / 2);
        const body = kind === "group"
          ? { kind, text: "Nhóm mới", x: cx - 160 + count * 16, y: cy - 110 + count * 16, width: 320, height: 220, color: GROUP_COLORS[count % 5] }
          : { kind, text: "Ghi chú", x: cx - 80 + count * 16, y: cy - 45 + count * 16, width: 170, height: 90, color: NOTE_COLORS[count % 5] };
        try {
          const note = await this.authFetch(this.topologyUrl("/notes"), { method: "POST", body: JSON.stringify(body) });
          this.notes = [...this.notes, note];
          this.selectedDecoId = note.id;
          this.multi = [];
          this.selectedNodeId = null;
        } catch (err) {
          this.topologyError = this.describeError(err);
        }
      },

      async saveNote(note) {
        try {
          await this.authFetch(this.topologyUrl(`/notes/${note.id}`), {
            method: "PUT",
            body: JSON.stringify({
              text: note.text, x: note.x, y: note.y, width: note.width, height: note.height, color: note.color,
            }),
          });
        } catch (err) {
          this.topologyError = this.describeError(err);
          await this.loadTopology();
        }
      },

      async deleteNote(note) {
        if (!note) return;
        try {
          await this.authFetch(this.topologyUrl(`/notes/${note.id}`), { method: "DELETE" });
          this.notes = this.notes.filter((n) => n.id !== note.id);
          this.selectedDecoId = null;
        } catch (err) {
          this.topologyError = this.describeError(err);
        }
      },

      setDecoColor(note, color) {
        note.color = color;
        this.saveNote(note);
      },

      decoPalette(note) {
        return note && note.kind === "group" ? GROUP_COLORS : NOTE_COLORS;
      },

      // ---- blueprints ------------------------------------------------------
      async openBlueprints() {
        this.blueprintError = "";
        this.blueprintOpen = true;
        if (!this.blueprints.length) {
          try {
            this.blueprints = (await this.authFetch(this.topologyUrl("/blueprints"))).items;
          } catch (err) {
            this.blueprintError = this.describeError(err);
          }
        }
        if (!this.blueprintKey && this.blueprints.length) this.pickBlueprint(this.blueprints[0].key);
      },

      pickBlueprint(key) {
        this.blueprintKey = key;
        const item = this.blueprints.find((b) => b.key === key);
        this.blueprintParams = Object.fromEntries((item ? item.params : []).map((p) => [p.name, p.default]));
      },

      get blueprintItem() {
        return this.blueprints.find((b) => b.key === this.blueprintKey) || null;
      },

      async buildBlueprint() {
        if (!this.blueprintKey || this.blueprintBusy) return;
        this.blueprintBusy = true;
        this.blueprintError = "";
        try {
          const params = {};
          for (const [k, v] of Object.entries(this.blueprintParams)) params[k] = Number(v) || 0;
          const result = await this.authFetch(this.topologyUrl(`/blueprints/${this.blueprintKey}`), {
            method: "POST",
            body: JSON.stringify({ params, planned: !!this.blueprintPlanned }),
          });
          await this.loadTopology();
          await this.loadDevices();
          await this.loadProjects();
          this.blueprintOpen = false;
          this.multi = result.devices.map((d) => d.id);
          this.$nextTick(() => this.fitView());
          this.topologyNotice = `Đã tạo ${result.devices.length} thiết bị và ${result.links.length} dây.`;
        } catch (err) {
          this.blueprintError = this.describeError(err);
        } finally {
          this.blueprintBusy = false;
        }
      },

      // ---- checks and addressing ------------------------------------------
      async runLint() {
        this.lintError = "";
        this.lintOpen = true;
        try {
          const [lint, ipam] = await Promise.all([
            this.authFetch(this.topologyUrl("/lint")),
            this.authFetch(this.topologyUrl("/ipam")),
          ]);
          this.lintResult = lint;
          this.ipamInfo = ipam;
        } catch (err) {
          this.lintError = this.describeError(err);
        }
      },

      focusIssue(issue) {
        if (issue.link_id) {
          this.multi = [];
          this.selectLink(issue.link_id);
        } else if (issue.device_id) {
          this.multi = [issue.device_id];
          this.selectedLinkId = null;
          this.selectedNodeId = issue.device_id;
          this.syncProps();
        }
        this.lintOpen = false;
      },

      // ---- planned -> provisioned -----------------------------------------
      async provisionSelected() {
        const p = this.props;
        if (!p) return;
        this.propsError = "";
        try {
          await this.authFetch(`/api/devices/${p.id}/provision`, {
            method: "POST",
            body: JSON.stringify({ management_ip: (p.management_ip || "").trim() }),
          });
          await this.loadTopology();
          await this.loadDevices();
          this.selectedNodeId = p.id;
          this.syncProps();
        } catch (err) {
          this.propsError = this.describeError(err);
        }
      },
    };
  };
})();
