function storedUser() {
  try {
    const user = JSON.parse(localStorage.getItem("nc_user") || "null");
    return user && typeof user === "object" ? user : null;
  } catch {
    localStorage.removeItem("nc_user");
    return null;
  }
}

function esc(value) {
  return String(value === null || value === undefined ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Canvas geometry. Nodes are drawn as NODE_W x NODE_H boxes centred on
// their (x, y); a device with no saved position gets a grid slot.
const NODE_W = 120;
const NODE_H = 52;
const CANVAS_W = 960;
const CANVAS_H = 560;

// Small line glyphs, drawn in a 24x24 box at the left of a node.
const ICONS = {
  router:
    '<circle cx="12" cy="12" r="8" /><path d="M12 5v14M5 12h14M9 8l3-3 3 3M9 16l3 3 3-3M8 9l-3 3 3 3M16 9l3 3-3 3" />',
  switch_l3:
    '<rect x="3" y="7" width="18" height="10" rx="2" /><path d="M7 11h10M14 8l3 3-3 3M10 16l-3-3" />',
  switch:
    '<rect x="3" y="7" width="18" height="10" rx="2" /><path d="M7 10.5h10M14 8l3 2.5-3 2.5M17 14H7M10 12l-3 2 3 2" />',
  firewall:
    '<rect x="3" y="5" width="18" height="14" rx="1" /><path d="M3 10h18M3 15h18M9 5v5M15 10v5M9 15v4" />',
};

function templateKeyFor(device) {
  if (device.device_type === "cisco_asa" || device.role === "firewall") return "firewall";
  if (device.role === "core") return "router";
  if (device.role === "distribution") return "switch_l3";
  return "switch";
}

const LINK_COLORS = { physical: "#75899f", routed: "#22c55e", trunk: "#06b6d4" };

const EMPTY_DEVICE_FORM = () => ({
  id: null,
  hostname: "",
  management_ip: "",
  device_type: "cisco_ios",
  role: "access",
  environment: "pnetlab",
  ssh_port: 22,
  monitoring_enabled: true,
  description: "",
  username: "",
  password: "",
  enable_secret: "",
});

const EMPTY_LINK_FORM = () => ({
  id: null,
  device_a_id: "",
  interface_a: "",
  device_b_id: "",
  interface_b: "",
  link_type: "physical",
  network: "",
  ip_a: "",
  ip_b: "",
  allowed_vlans: "",
  bring_up: true,
  description: "",
});

document.addEventListener("alpine:init", () => {
  Alpine.data("projectsApp", () => ({
    // -- auth --
    token: localStorage.getItem("nc_token") || null,
    currentUser: storedUser(),
    loginForm: { username: "", password: "" },
    loginError: "",
    _sessionGeneration: 0,

    // -- projects --
    projects: [],
    currentProjectId: (() => {
      const stored = localStorage.getItem("nc_project_id");
      return stored ? Number(stored) : null;
    })(),
    projectForm: {
      name: "",
      description: "",
      management_network: "",
      environment: "pnetlab",
    },
    settingsForm: null,
    projectError: "",
    projectNotice: "",
    tab: "devices",

    // -- devices --
    devices: [],
    deviceForm: EMPTY_DEVICE_FORM(),
    deviceFormOpen: false,
    deviceError: "",
    deviceNotice: "",
    deviceBusy: {},

    // -- sharing --
    members: [],
    shareForm: { username: "", access: "viewer" },
    shareError: "",

    // -- topology --
    links: [],
    selectedNodeId: null,
    selectedLinkId: null,
    linkMode: false,
    linkForm: EMPTY_LINK_FORM(),
    linkFormOpen: false,
    topologyError: "",
    templates: [],
    defaultSsh: { username: "", password: "" },
    dropBusy: false,
    linkSourceId: null,
    plan: null,
    previewNotice: "",
    previewBatchId: null,
    _drag: null,
    _suppressClick: false,

    // ------------------------------------------------------------------
    // lifecycle / auth
    // ------------------------------------------------------------------

    init() {
      window.addEventListener("keydown", (event) => this.onKeyDown(event));
      if (!this.token) return;
      this.startApp();
    },

    // Delete removes what is selected on the canvas, like a diagram editor.
    onKeyDown(event) {
      if (event.key === "Escape" && this.tab === "topology") {
        this.linkMode = false;
        this.linkSourceId = null;
        this.closeLinkForm();
        return;
      }
      if (event.key !== "Delete" || this.tab !== "topology" || !this.canEdit) return;
      const tag = (event.target && event.target.tagName) || "";
      if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) return;
      if (this.selectedLinkId) {
        this.linkForm.id = this.selectedLinkId;
        this.deleteLink();
      } else if (this.selectedNode) {
        this.deleteDevice(this.selectedNode);
      }
    },

    async authFetch(path, options = {}) {
      const token = this.token;
      const generation = this._sessionGeneration;
      const projectId = this.currentProjectId;
      const headers = Object.assign(
        { "Content-Type": "application/json" },
        projectId ? { "X-Project-Id": String(projectId) } : {},
        options.headers || {},
        token ? { Authorization: `Bearer ${token}` } : {}
      );
      const response = await fetch(path, { ...options, headers });
      const data =
        response.status === 204 ? {} : await response.json().catch(() => ({}));
      if (
        response.status === 401 &&
        generation === this._sessionGeneration &&
        token === this.token
      ) {
        this.logout();
      }
      if (!response.ok) {
        const error = new Error(
          (data && data.message) || `Request failed (${response.status})`
        );
        error.status = response.status;
        error.data = data;
        throw error;
      }
      return data;
    },

    describeError(err) {
      const details = err && err.data && err.data.details;
      if (details && typeof details === "object") {
        const parts = [];
        for (const [field, messages] of Object.entries(details)) {
          if (Array.isArray(messages)) parts.push(`${field}: ${messages.join(", ")}`);
        }
        if (parts.length) return `${err.message} (${parts.join("; ")})`;
      }
      return (err && err.message) || "Có lỗi xảy ra.";
    },

    async login() {
      this.loginError = "";
      const generation = this._sessionGeneration;
      try {
        const data = await this.authFetch("/api/auth/login", {
          method: "POST",
          body: JSON.stringify(this.loginForm),
        });
        if (generation !== this._sessionGeneration) return;
        this.token = data.access_token;
        this.currentUser = data.user;
        localStorage.setItem("nc_token", this.token);
        localStorage.setItem("nc_user", JSON.stringify(this.currentUser));
        this.loginForm = { username: "", password: "" };
        await this.startApp();
      } catch (err) {
        this.loginError = err.message || "Đăng nhập thất bại.";
      }
    },

    logout() {
      this._sessionGeneration += 1;
      this.token = null;
      this.currentUser = null;
      localStorage.removeItem("nc_token");
      localStorage.removeItem("nc_user");
      localStorage.removeItem("nc_project_id");
      this.projects = [];
      this.currentProjectId = null;
      this._clearProjectData();
    },

    get isAdmin() {
      return !!this.currentUser && this.currentUser.role === "ADMIN";
    },

    get project() {
      return this.projects.find((p) => p.id === this.currentProjectId) || null;
    },

    get canEdit() {
      const project = this.project;
      return (
        !!project &&
        (project.access === "owner" || project.access === "editor") &&
        !!this.currentUser &&
        this.currentUser.role !== "VIEWER"
      );
    },

    get isOwner() {
      return !!this.project && this.project.access === "owner";
    },

    get canCreateProject() {
      return !!this.currentUser && this.currentUser.role !== "VIEWER";
    },

    async startApp() {
      const generation = this._sessionGeneration;
      try {
        await this.loadProjects();
        if (generation !== this._sessionGeneration) return;
        if (this.currentProjectId) await this.loadProjectData();
      } catch (err) {
        if (generation === this._sessionGeneration) {
          this.projectError = this.describeError(err);
        }
      }
    },

    // ------------------------------------------------------------------
    // projects
    // ------------------------------------------------------------------

    async loadProjects() {
      const data = await this.authFetch("/api/projects");
      this.projects = data.items;
      if (!this.projects.some((p) => p.id === this.currentProjectId)) {
        this.currentProjectId = this.projects.length ? this.projects[0].id : null;
      }
      if (this.currentProjectId) {
        localStorage.setItem("nc_project_id", String(this.currentProjectId));
      } else {
        localStorage.removeItem("nc_project_id");
      }
      this.syncSettingsForm();
    },

    syncSettingsForm() {
      const project = this.project;
      this.settingsForm = project
        ? {
            name: project.name,
            description: project.description || "",
            management_network: project.management_network,
            environment: project.environment,
          }
        : null;
    },

    _clearProjectData() {
      this.devices = [];
      this.links = [];
      this.members = [];
      this.plan = null;
      this.previewNotice = "";
      this.previewBatchId = null;
      this.selectedNodeId = null;
      this.selectedLinkId = null;
      this.linkMode = false;
      this.linkSourceId = null;
      this.templates = [];
      this.linkFormOpen = false;
      this.deviceFormOpen = false;
      this.deviceError = "";
      this.deviceNotice = "";
      this.topologyError = "";
      this.shareError = "";
      this.projectError = "";
      this.projectNotice = "";
    },

    async selectProject(projectId) {
      if (projectId === this.currentProjectId) return;
      this._clearProjectData();
      this.currentProjectId = projectId;
      localStorage.setItem("nc_project_id", String(projectId));
      this.syncSettingsForm();
      try {
        await this.loadProjectData();
      } catch (err) {
        this.projectError = this.describeError(err);
      }
    },

    async loadProjectData() {
      const projectId = this.currentProjectId;
      await this.loadDevices();
      if (projectId !== this.currentProjectId) return;
      await this.loadTopology();
      if (projectId !== this.currentProjectId) return;
      await this.loadTemplates();
      if (projectId !== this.currentProjectId) return;
      await this.loadMembers();
    },

    async createProject() {
      this.projectError = "";
      try {
        const created = await this.authFetch("/api/projects", {
          method: "POST",
          body: JSON.stringify({
            name: this.projectForm.name,
            description: this.projectForm.description || null,
            management_network: this.projectForm.management_network,
            environment: this.projectForm.environment,
          }),
        });
        this.projectForm = {
          name: "",
          description: "",
          management_network: "",
          environment: "pnetlab",
        };
        await this.loadProjects();
        await this.selectProject(created.id);
        this.projectNotice = `Đã tạo project ${created.name}.`;
      } catch (err) {
        this.projectError = this.describeError(err);
      }
    },

    async saveSettings() {
      this.projectError = "";
      this.projectNotice = "";
      try {
        await this.authFetch(`/api/projects/${this.currentProjectId}`, {
          method: "PUT",
          body: JSON.stringify({
            name: this.settingsForm.name,
            description: this.settingsForm.description || null,
            management_network: this.settingsForm.management_network,
            environment: this.settingsForm.environment,
          }),
        });
        await this.loadProjects();
        this.projectNotice = "Đã lưu cài đặt.";
      } catch (err) {
        this.projectError = this.describeError(err);
      }
    },

    async deleteProject() {
      const project = this.project;
      if (!project) return;
      const typed = window.prompt(
        `Xoá project sẽ xoá toàn bộ thiết bị, sơ đồ, chat và lịch sử thay đổi của nó.\nNhập đúng tên project để xác nhận: ${project.name}`
      );
      if (typed !== project.name) return;
      this.projectError = "";
      try {
        await this.authFetch(`/api/projects/${project.id}`, { method: "DELETE" });
        this._clearProjectData();
        this.currentProjectId = null;
        await this.loadProjects();
        if (this.currentProjectId) await this.loadProjectData();
      } catch (err) {
        this.projectError = this.describeError(err);
      }
    },

    // ------------------------------------------------------------------
    // devices
    // ------------------------------------------------------------------

    async loadDevices() {
      const projectId = this.currentProjectId;
      const data = await this.authFetch("/api/devices");
      if (projectId === this.currentProjectId) this.devices = data.items;
    },

    openDeviceForm(device) {
      this.deviceError = "";
      this.deviceNotice = "";
      if (device) {
        this.deviceForm = {
          ...EMPTY_DEVICE_FORM(),
          id: device.id,
          hostname: device.hostname,
          management_ip: device.management_ip,
          device_type: device.device_type,
          role: device.role,
          environment: device.environment,
          ssh_port: device.ssh_port,
          monitoring_enabled: device.monitoring_enabled,
          description: device.description || "",
        };
      } else {
        this.deviceForm = EMPTY_DEVICE_FORM();
        if (this.project && this.project.environment !== "mixed") {
          this.deviceForm.environment = this.project.environment;
        }
      }
      this.deviceFormOpen = true;
    },

    closeDeviceForm() {
      this.deviceFormOpen = false;
      this.deviceForm = EMPTY_DEVICE_FORM();
    },

    async saveDevice() {
      this.deviceError = "";
      this.deviceNotice = "";
      const form = this.deviceForm;
      const payload = {
        hostname: form.hostname.trim(),
        management_ip: form.management_ip.trim(),
        device_type: form.device_type,
        role: form.role,
        environment: form.environment,
        ssh_port: Number(form.ssh_port) || 22,
        monitoring_enabled: !!form.monitoring_enabled,
        description: form.description || null,
      };
      // A blank password on edit means "keep the stored credential".
      if (form.username && form.password) {
        payload.credential = {
          username: form.username,
          password: form.password,
          enable_secret: form.enable_secret || null,
        };
      } else if (form.username || form.password) {
        this.deviceError = "Nhập cả tên đăng nhập và mật khẩu SSH, hoặc để trống cả hai.";
        return;
      }
      try {
        if (form.id) {
          await this.authFetch(`/api/devices/${form.id}`, {
            method: "PUT",
            body: JSON.stringify(payload),
          });
          this.deviceNotice = `Đã cập nhật ${payload.hostname}.`;
        } else {
          await this.authFetch("/api/devices", {
            method: "POST",
            body: JSON.stringify(payload),
          });
          this.deviceNotice = `Đã thêm ${payload.hostname}.`;
        }
        this.closeDeviceForm();
        await this.loadDevices();
        await this.loadTopology();
        await this.loadProjects();
      } catch (err) {
        this.deviceError = this.describeError(err);
      }
    },

    async deleteDevice(device) {
      if (!window.confirm(`Xoá thiết bị ${device.hostname} và các liên kết của nó?`)) {
        return;
      }
      this.deviceError = "";
      this.deviceNotice = "";
      try {
        await this.authFetch(`/api/devices/${device.id}`, { method: "DELETE" });
        await this.loadDevices();
        await this.loadTopology();
        await this.loadProjects();
        this.deviceNotice = `Đã xoá ${device.hostname}.`;
      } catch (err) {
        this.deviceError = this.describeError(err);
      }
    },

    async testConnection(device) {
      this.deviceError = "";
      this.deviceNotice = "";
      this.deviceBusy = { ...this.deviceBusy, [device.id]: true };
      try {
        const result = await this.authFetch(
          `/api/devices/${device.id}/test-connection`,
          { method: "POST", body: "{}" }
        );
        this.deviceNotice = `${device.hostname}: ${result.detail}`;
        await this.loadDevices();
      } catch (err) {
        this.deviceError = this.describeError(err);
      } finally {
        const busy = { ...this.deviceBusy };
        delete busy[device.id];
        this.deviceBusy = busy;
      }
    },

    // ------------------------------------------------------------------
    // sharing
    // ------------------------------------------------------------------

    async loadMembers() {
      const projectId = this.currentProjectId;
      const data = await this.authFetch(`/api/projects/${projectId}/members`);
      if (projectId === this.currentProjectId) this.members = data.items;
    },

    async shareProject() {
      this.shareError = "";
      try {
        await this.authFetch(`/api/projects/${this.currentProjectId}/members`, {
          method: "POST",
          body: JSON.stringify({
            username: this.shareForm.username.trim(),
            access: this.shareForm.access,
          }),
        });
        this.shareForm = { username: "", access: "viewer" };
        await this.loadMembers();
      } catch (err) {
        this.shareError = this.describeError(err);
      }
    },

    async changeMemberAccess(member, access) {
      this.shareError = "";
      try {
        await this.authFetch(
          `/api/projects/${this.currentProjectId}/members/${member.user_id}`,
          { method: "PUT", body: JSON.stringify({ access }) }
        );
        await this.loadMembers();
      } catch (err) {
        this.shareError = this.describeError(err);
      }
    },

    async unshare(member) {
      this.shareError = "";
      try {
        await this.authFetch(
          `/api/projects/${this.currentProjectId}/members/${member.user_id}`,
          { method: "DELETE" }
        );
        await this.loadMembers();
      } catch (err) {
        this.shareError = this.describeError(err);
      }
    },

    // ------------------------------------------------------------------
    // topology
    // ------------------------------------------------------------------

    topologyUrl(suffix = "") {
      return `/api/projects/${this.currentProjectId}/topology${suffix}`;
    },

    async loadTopology() {
      const projectId = this.currentProjectId;
      const data = await this.authFetch(this.topologyUrl());
      if (projectId !== this.currentProjectId) return;
      this.devices = data.nodes;
      this.links = data.links;
      if (this.selectedLinkId && !this.links.some((l) => l.id === this.selectedLinkId)) {
        this.selectedLinkId = null;
      }
    },

    deviceById(id) {
      return this.devices.find((d) => d.id === Number(id)) || null;
    },

    // Effective centre of a node; devices never placed get a grid slot.
    nodePos(device) {
      if (device.pos_x !== null && device.pos_x !== undefined) {
        return { x: device.pos_x, y: device.pos_y };
      }
      const index = this.devices.indexOf(device);
      const columns = 5;
      return {
        x: 100 + (index % columns) * 190,
        y: 90 + Math.floor(index / columns) * 150,
      };
    },

    // The canvas grows to fit devices dragged past the default edge.
    get canvasW() {
      const xs = this.devices.map((d) => this.nodePos(d).x);
      return Math.max(CANVAS_W, Math.min(4000, Math.max(0, ...xs) + NODE_W));
    },

    get canvasH() {
      const ys = this.devices.map((d) => this.nodePos(d).y);
      return Math.max(CANVAS_H, Math.min(4000, Math.max(0, ...ys) + NODE_H * 2));
    },

    get topologySvg() {
      const parts = [];
      const pos = {};
      for (const device of this.devices) pos[device.id] = this.nodePos(device);

      // Spread parallel links between the same pair of devices.
      const seen = {};
      for (const link of this.links) {
        const a = pos[link.device_a_id];
        const b = pos[link.device_b_id];
        if (!a || !b) continue;
        const key = [link.device_a_id, link.device_b_id].sort().join("-");
        const slot = (seen[key] = (seen[key] || 0) + 1) - 1;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const length = Math.hypot(dx, dy) || 1;
        const nx = (-dy / length) * slot * 14;
        const ny = (dx / length) * slot * 14;
        const x1 = a.x + nx;
        const y1 = a.y + ny;
        const x2 = b.x + nx;
        const y2 = b.y + ny;
        const color = LINK_COLORS[link.link_type] || LINK_COLORS.physical;
        const selected = link.id === this.selectedLinkId;
        const dash = link.link_type === "trunk" ? ' stroke-dasharray="7 4"' : "";
        parts.push(
          `<g class="topo-link${selected ? " selected" : ""}" data-link-id="${link.id}">` +
            `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="transparent" stroke-width="16" />` +
            `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${color}" stroke-width="${selected ? 4 : 2}"${dash} />` +
            this._linkLabels(link, x1, y1, x2, y2, color) +
            `</g>`
        );
      }

      for (const device of this.devices) {
        const p = pos[device.id];
        const selected =
          device.id === this.selectedNodeId || device.id === this.linkSourceId;
        const status = ["online", "offline"].includes(device.status)
          ? device.status
          : "unknown";
        parts.push(
          `<g class="topo-node${selected ? " selected" : ""} env-${esc(device.environment)}" data-node-id="${device.id}" transform="translate(${p.x - NODE_W / 2},${p.y - NODE_H / 2})">` +
            `<rect width="${NODE_W}" height="${NODE_H}" rx="8" />` +
            `<circle class="dot ${status}" cx="${NODE_W - 10}" cy="10" r="4.5" />` +
            `<g class="icon" transform="translate(6,${NODE_H / 2 - 14}) scale(1.15)">${ICONS[templateKeyFor(device)]}</g>` +
            `<text class="host" x="${NODE_W / 2 + 10}" y="24" text-anchor="middle">${esc(device.hostname)}</text>` +
            `<text class="sub" x="${NODE_W / 2 + 10}" y="41" text-anchor="middle">${esc(device.role)} · ${device.environment === "physical" ? "thật" : "PNETLab"}</text>` +
            `</g>`
        );
      }
      return parts.join("");
    },

    _linkLabels(link, x1, y1, x2, y2, color) {
      const at = (t) => ({ x: x1 + (x2 - x1) * t, y: y1 + (y2 - y1) * t });
      const near = at(0.24);
      const far = at(0.76);
      const mid = at(0.5);
      let middle = "";
      if (link.link_type === "routed" && link.network) middle = link.network;
      else if (link.link_type === "trunk") {
        middle = link.allowed_vlans ? `trunk ${link.allowed_vlans}` : "trunk";
      }
      const ifaceA = esc(this.shortInterface(link.interface_a));
      const ifaceB = esc(this.shortInterface(link.interface_b));
      const ipA = link.ip_a ? ` ${esc(link.ip_a)}` : "";
      const ipB = link.ip_b ? ` ${esc(link.ip_b)}` : "";
      return (
        `<text class="iface" x="${near.x}" y="${near.y - 6}" text-anchor="middle">${ifaceA}${ipA}</text>` +
        `<text class="iface" x="${far.x}" y="${far.y - 6}" text-anchor="middle">${ifaceB}${ipB}</text>` +
        (middle
          ? `<text class="mid" x="${mid.x}" y="${mid.y + 14}" text-anchor="middle" fill="${color}">${esc(middle)}</text>`
          : "")
      );
    },

    shortInterface(name) {
      return String(name)
        .replace(/^GigabitEthernet/i, "Gi")
        .replace(/^FastEthernet/i, "Fa")
        .replace(/^TenGigabitEthernet/i, "Te")
        .replace(/^Ethernet/i, "Eth");
    },

    _svgPoint(event) {
      const svg = this.$refs.canvas;
      const point = svg.createSVGPoint();
      point.x = event.clientX;
      point.y = event.clientY;
      const mapped = point.matrixTransform(svg.getScreenCTM().inverse());
      return { x: mapped.x, y: mapped.y };
    },

    onCanvasPointerDown(event) {
      const nodeEl = event.target.closest("[data-node-id]");
      const linkEl = event.target.closest("[data-link-id]");
      if (nodeEl) {
        const id = Number(nodeEl.dataset.nodeId);
        const device = this.deviceById(id);
        if (!device) return;
        const start = this._svgPoint(event);
        const origin = this.nodePos(device);
        this._drag = {
          id,
          dx: origin.x - start.x,
          dy: origin.y - start.y,
          startX: event.clientX,
          startY: event.clientY,
          moved: false,
        };
        const move = (e) => this._onDragMove(e);
        const up = (e) => {
          window.removeEventListener("pointermove", move);
          window.removeEventListener("pointerup", up);
          this._onDragEnd(e);
        };
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", up);
        event.preventDefault();
      } else if (linkEl) {
        this.selectLink(Number(linkEl.dataset.linkId));
      } else {
        this.selectedNodeId = null;
        this.selectedLinkId = null;
      }
    },

    _onDragMove(event) {
      const drag = this._drag;
      if (!drag) return;
      if (
        !drag.moved &&
        Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < 4
      ) {
        return;
      }
      if (!this.canEdit) return;
      drag.moved = true;
      const point = this._svgPoint(event);
      const device = this.deviceById(drag.id);
      if (!device) return;
      device.pos_x = Math.max(NODE_W / 2, Math.min(4000, point.x + drag.dx));
      device.pos_y = Math.max(NODE_H / 2, Math.min(4000, point.y + drag.dy));
    },

    async _onDragEnd() {
      const drag = this._drag;
      this._drag = null;
      if (!drag) return;
      const device = this.deviceById(drag.id);
      if (!device) return;
      if (!drag.moved) {
        this.clickNode(device);
        return;
      }
      try {
        await this.authFetch(this.topologyUrl("/layout"), {
          method: "PUT",
          body: JSON.stringify({
            positions: [{ device_id: device.id, x: device.pos_x, y: device.pos_y }],
          }),
        });
      } catch (err) {
        this.topologyError = this.describeError(err);
      }
    },

    clickNode(device) {
      this.topologyError = "";
      if (this.linkMode && this.canEdit) {
        // Quick cabling: click the source, then the target. Free ports are
        // picked for both ends; click the new link afterwards to refine it.
        if (!this.linkSourceId) {
          this.linkSourceId = device.id;
        } else if (device.id !== this.linkSourceId) {
          const source = this.linkSourceId;
          this.linkSourceId = null;
          this.quickLink(source, device.id);
        }
        return;
      }
      this.selectedLinkId = null;
      this.selectedNodeId = device.id;
    },

    get selectedNode() {
      return this.deviceById(this.selectedNodeId);
    },

    get selectedLink() {
      return this.links.find((l) => l.id === this.selectedLinkId) || null;
    },

    toggleLinkMode() {
      this.linkMode = !this.linkMode;
      this.linkSourceId = null;
      this.topologyError = "";
      this.selectedNodeId = null;
      this.selectedLinkId = null;
      this.linkFormOpen = false;
    },

    async quickLink(sourceId, targetId) {
      this.topologyError = "";
      try {
        await this.authFetch(this.topologyUrl("/links"), {
          method: "POST",
          body: JSON.stringify({ device_a_id: sourceId, device_b_id: targetId }),
        });
        this.plan = null;
        await this.loadTopology();
      } catch (err) {
        this.topologyError = this.describeError(err);
      }
    },

    openManualLinkForm() {
      this.linkMode = false;
      this.linkSourceId = null;
      this.selectedLinkId = null;
      this.linkForm = EMPTY_LINK_FORM();
      this.linkFormOpen = true;
    },

    // ---- palette: drag a device type onto the canvas --------------------

    async loadTemplates() {
      const projectId = this.currentProjectId;
      const data = await this.authFetch(this.topologyUrl("/templates"));
      if (projectId === this.currentProjectId) this.templates = data.items;
    },

    iconSvg(key) {
      return `<svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${ICONS[key] || ""}</svg>`;
    },

    onPaletteDragStart(event, key) {
      event.dataTransfer.setData("text/plain", `template:${key}`);
      event.dataTransfer.effectAllowed = "copy";
    },

    onCanvasDrop(event) {
      event.preventDefault();
      const raw = event.dataTransfer.getData("text/plain");
      if (!raw.startsWith("template:") || !this.canEdit) return;
      const point = this._svgPoint(event);
      this.addTemplate(raw.slice("template:".length), point);
    },

    async addTemplate(key, point) {
      if (!this.canEdit || this.dropBusy) return;
      this.dropBusy = true;
      this.topologyError = "";
      const body = { template: key };
      if (point) {
        body.x = Math.max(NODE_W / 2, Math.round(point.x));
        body.y = Math.max(NODE_H / 2, Math.round(point.y));
      }
      if (this.defaultSsh.username && this.defaultSsh.password) {
        body.credential = { ...this.defaultSsh };
      }
      try {
        const device = await this.authFetch(this.topologyUrl("/quick-device"), {
          method: "POST",
          body: JSON.stringify(body),
        });
        await this.loadTopology();
        await this.loadProjects();
        this.selectedLinkId = null;
        this.selectedNodeId = device.id;
      } catch (err) {
        this.topologyError = this.describeError(err);
      } finally {
        this.dropBusy = false;
      }
    },

    editSelectedDevice() {
      const device = this.selectedNode;
      if (!device) return;
      this.tab = "devices";
      this.openDeviceForm(device);
    },

    selectLink(id) {
      const link = this.links.find((l) => l.id === id);
      if (!link) return;
      this.linkMode = false;
      this.selectedNodeId = null;
      this.selectedLinkId = id;
      this.topologyError = "";
      this.linkForm = {
        id: link.id,
        device_a_id: link.device_a_id,
        interface_a: link.interface_a,
        device_b_id: link.device_b_id,
        interface_b: link.interface_b,
        link_type: link.link_type,
        network: link.network || "",
        ip_a: link.ip_a || "",
        ip_b: link.ip_b || "",
        allowed_vlans: link.allowed_vlans || "",
        bring_up: link.bring_up,
        description: link.description || "",
      };
      this.linkFormOpen = true;
    },

    closeLinkForm() {
      this.linkFormOpen = false;
      this.linkMode = false;
      this.selectedLinkId = null;
      this.linkForm = EMPTY_LINK_FORM();
    },

    _linkPayload(creating) {
      const form = this.linkForm;
      const typed = form.link_type;
      const payload = {
        link_type: typed,
        bring_up: !!form.bring_up,
        description: form.description || null,
      };
      if (typed === "routed") {
        payload.network = form.network.trim() || null;
        payload.ip_a = form.ip_a.trim() || null;
        payload.ip_b = form.ip_b.trim() || null;
      }
      if (typed === "trunk") payload.allowed_vlans = form.allowed_vlans.trim() || null;
      if (creating) {
        payload.device_a_id = Number(form.device_a_id);
        payload.interface_a = form.interface_a;
        payload.device_b_id = Number(form.device_b_id);
        payload.interface_b = form.interface_b;
      }
      return payload;
    },

    async saveLink() {
      this.topologyError = "";
      const form = this.linkForm;
      try {
        if (form.id) {
          await this.authFetch(this.topologyUrl(`/links/${form.id}`), {
            method: "PUT",
            body: JSON.stringify(this._linkPayload(false)),
          });
        } else {
          if (!form.device_a_id || !form.device_b_id) {
            this.topologyError = "Chọn hai thiết bị cần nối.";
            return;
          }
          await this.authFetch(this.topologyUrl("/links"), {
            method: "POST",
            body: JSON.stringify(this._linkPayload(true)),
          });
        }
        this.plan = null;
        await this.loadTopology();
        this.closeLinkForm();
      } catch (err) {
        this.topologyError = this.describeError(err);
      }
    },

    async deleteLink() {
      const id = this.linkForm.id;
      if (!id) return;
      this.topologyError = "";
      try {
        await this.authFetch(this.topologyUrl(`/links/${id}`), { method: "DELETE" });
        this.plan = null;
        await this.loadTopology();
        this.closeLinkForm();
      } catch (err) {
        this.topologyError = this.describeError(err);
      }
    },

    async autoLayout() {
      if (!this.canEdit || !this.devices.length) return;
      this.topologyError = "";
      const columns = 5;
      const positions = this.devices.map((device, index) => {
        device.pos_x = 100 + (index % columns) * 190;
        device.pos_y = 90 + Math.floor(index / columns) * 150;
        return { device_id: device.id, x: device.pos_x, y: device.pos_y };
      });
      try {
        await this.authFetch(this.topologyUrl("/layout"), {
          method: "PUT",
          body: JSON.stringify({ positions }),
        });
      } catch (err) {
        this.topologyError = this.describeError(err);
      }
    },

    async loadPlan() {
      this.topologyError = "";
      this.previewNotice = "";
      this.plan = null;
      try {
        this.plan = await this.authFetch(this.topologyUrl("/config-plan"), {
          method: "POST",
          body: "{}",
        });
      } catch (err) {
        this.topologyError = this.describeError(err);
      }
    },

    async createPreview() {
      this.topologyError = "";
      this.previewNotice = "";
      this.previewBatchId = null;
      try {
        const result = await this.authFetch(this.topologyUrl("/config-preview"), {
          method: "POST",
          body: "{}",
        });
        this.previewBatchId = result.batch.id;
        const count = result.batch.changes.length;
        this.previewNotice = `Đã tạo bản xem trước #${result.batch.id} cho ${count} thiết bị. Duyệt và áp dụng nó ở trang Chat.`;
        if (result.skipped.length) {
          this.previewNotice += ` Bỏ qua: ${result.skipped.map((s) => s.hostname).join(", ")}.`;
        }
      } catch (err) {
        this.topologyError = this.describeError(err);
      }
    },
  }));
});
