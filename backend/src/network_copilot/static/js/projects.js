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

const EMPTY_DESIGN = () => ({ vlans: [], access_ports: [], svis: [], routes: [], ospf: [] });

const EMPTY_DESIGN_FORMS = () => ({
  vlans: { vlan_id: "", name: "" },
  "access-ports": { device_id: "", interface: "", vlan_id: "" },
  svis: { device_id: "", vlan_id: "", ip: "", prefix_length: 24 },
  routes: { device_id: "", network: "", next_hop: "" },
  ospf: { device_id: "", process_id: 1, router_id: "" },
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
  nameif_a: "",
  security_a: "",
  nameif_b: "",
  security_b: "",
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

    // -- network design (VLANs, ports, SVIs, routes, OSPF) --
    design: EMPTY_DESIGN(),
    designForms: EMPTY_DESIGN_FORMS(),
    designError: "",

    // -- PNETLab import --
    pnet: null,

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
    discovery: null,
    multi: [],
    band: null,
    undoStack: [],
    redoStack: [],
    historyBusy: false,
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
        this.multi = [];
        this.selectedNodeId = null;
        this.closeLinkForm();
        return;
      }
      if (this.tab !== "topology" || !this.canEdit) return;
      const tag = (event.target && event.target.tagName) || "";
      if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) return;
      const key = event.key.toLowerCase();
      if ((event.ctrlKey || event.metaKey) && key === "z") {
        event.preventDefault();
        if (event.shiftKey) this.redo();
        else this.undo();
        return;
      }
      if ((event.ctrlKey || event.metaKey) && key === "y") {
        event.preventDefault();
        this.redo();
        return;
      }
      if ((event.ctrlKey || event.metaKey) && key === "a") {
        event.preventDefault();
        this.multi = this.devices.map((d) => d.id);
        return;
      }
      if (event.key !== "Delete") return;
      if (this.selectedLinkId) {
        this.linkForm.id = this.selectedLinkId;
        this.deleteLink();
      } else if (this.multi.length > 1) {
        this.deleteSelectedDevices();
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
      this.design = EMPTY_DESIGN();
      this.designError = "";
      this.undoStack = [];
      this.redoStack = [];
      this.multi = [];
      this.discovery = null;
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
      await this.loadDesign();
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
        // Earlier steps may refer to this device or its cables: history ends here.
        this.undoStack = [];
        this.redoStack = [];
        this.multi = [];
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
    // import / export / clone
    // ------------------------------------------------------------------

    async _download(path, filename) {
      const response = await fetch(path, {
        headers: { Authorization: `Bearer ${this.token}`, "X-Project-Id": String(this.currentProjectId) },
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.message || `Request failed (${response.status})`);
      }
      this._saveBlob(await response.blob(), filename);
    },

    _saveBlob(blob, filename) {
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    },

    _slug() {
      return (this.project ? this.project.name : "project").replace(/[^A-Za-z0-9_-]+/g, "_");
    },

    async exportProject() {
      this.projectError = "";
      try {
        await this._download(`/api/projects/${this.currentProjectId}/export`, `${this._slug()}.json`);
      } catch (err) {
        this.projectError = this.describeError(err);
      }
    },

    async exportDevicesCsv() {
      this.deviceError = "";
      try {
        await this._download(`/api/projects/${this.currentProjectId}/devices.csv`, `${this._slug()}-devices.csv`);
      } catch (err) {
        this.deviceError = this.describeError(err);
      }
    },

    async cloneProject() {
      const name = window.prompt("Tên project mới:", `${this.project.name} (copy)`);
      if (!name) return;
      this.projectError = "";
      try {
        const copy = await this.authFetch(`/api/projects/${this.currentProjectId}/clone`, {
          method: "POST",
          body: JSON.stringify({ name }),
        });
        await this.loadProjects();
        await this.selectProject(copy.id);
        this.projectNotice = `Đã nhân bản thành ${copy.name} (không kèm mật khẩu SSH, người được chia sẻ hay lịch sử).`;
      } catch (err) {
        this.projectError = this.describeError(err);
      }
    },

    async importProjectFile(event) {
      const file = event.target.files[0];
      event.target.value = "";
      if (!file) return;
      this.projectError = "";
      try {
        const document_ = JSON.parse(await file.text());
        const created = await this.authFetch("/api/projects/import", {
          method: "POST",
          body: JSON.stringify({ document: document_ }),
        });
        await this.loadProjects();
        await this.selectProject(created.id);
        this.projectNotice = `Đã nhập project ${created.name}. Thêm lại mật khẩu SSH cho các thiết bị.`;
      } catch (err) {
        this.projectError = err instanceof SyntaxError ? "File không phải JSON hợp lệ." : this.describeError(err);
      }
    },

    async importCsvFile(event) {
      const file = event.target.files[0];
      event.target.value = "";
      if (!file) return;
      this.deviceError = "";
      this.deviceNotice = "";
      try {
        const result = await this.authFetch(`/api/projects/${this.currentProjectId}/devices/import-csv`, {
          method: "POST",
          body: JSON.stringify({ csv: await file.text() }),
        });
        this.deviceNotice = `Đã nhập ${result.created.length} thiết bị từ CSV.`;
        await this.loadDevices();
        await this.loadTopology();
        await this.loadProjects();
      } catch (err) {
        this.deviceError = this.describeError(err);
      }
    },

    // The diagram as a standalone SVG (dark theme colours inlined) or PNG.
    _diagramSvg() {
      const svg = this.$refs.canvas.cloneNode(true);
      const width = this.canvasW;
      const height = this.canvasH;
      svg.setAttribute("xmlns", "http://www.w3.org/2000/svg");
      svg.setAttribute("width", width);
      svg.setAttribute("height", height);
      svg.removeAttribute("class");
      for (const attribute of [...svg.attributes]) {
        if (attribute.name.startsWith("@") || attribute.name.startsWith(":") || attribute.name.startsWith("x-")) {
          svg.removeAttribute(attribute.name);
        }
      }
      const style = document.createElementNS("http://www.w3.org/2000/svg", "style");
      style.textContent =
        "text{font-family:Arial,sans-serif}" +
        ".topo-node rect{fill:#131f30;stroke:#223349;stroke-width:1.5}" +
        ".topo-node.env-physical rect{stroke:#f0c674}" +
        ".topo-node .host{fill:#dbe4ee;font-size:14px;font-weight:600}" +
        ".topo-node .sub{fill:#75899f;font-size:10px}" +
        ".topo-node .icon{fill:none;stroke:#06b6d4;stroke-width:1.4;stroke-linecap:round;stroke-linejoin:round}" +
        ".topo-node .dot.online{fill:#22c55e}.topo-node .dot.offline{fill:#ef4444}.topo-node .dot.unknown{fill:#6f8299}" +
        ".topo-link .iface{fill:#dbe4ee;font-size:10px}.topo-link .mid{font-size:11px}";
      const background = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      background.setAttribute("width", "100%");
      background.setAttribute("height", "100%");
      background.setAttribute("fill", "#0b1420");
      svg.insertBefore(background, svg.firstChild);
      svg.insertBefore(style, svg.firstChild);
      return { markup: new XMLSerializer().serializeToString(svg), width, height };
    },

    exportDiagramSvg() {
      const { markup } = this._diagramSvg();
      this._saveBlob(new Blob([markup], { type: "image/svg+xml" }), `${this._slug()}-topology.svg`);
    },

    exportDiagramPng() {
      const { markup, width, height } = this._diagramSvg();
      const image = new Image();
      image.onload = () => {
        const canvas = document.createElement("canvas");
        canvas.width = width * 2;
        canvas.height = height * 2;
        const context = canvas.getContext("2d");
        context.scale(2, 2);
        context.drawImage(image, 0, 0, width, height);
        canvas.toBlob((blob) => blob && this._saveBlob(blob, `${this._slug()}-topology.png`));
      };
      image.onerror = () => {
        this.topologyError = "Không xuất được ảnh PNG; thử xuất SVG.";
      };
      image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(markup)}`;
    },

    // ------------------------------------------------------------------
    // design: VLANs, access ports, SVIs, static routes, OSPF
    // ------------------------------------------------------------------

    get switchDevices() {
      return this.devices.filter(
        (d) => d.device_type === "cisco_ios" && ["access", "distribution"].includes(d.role)
      );
    },

    get iosDevices() {
      return this.devices.filter((d) => d.device_type === "cisco_ios");
    },

    async loadDesign() {
      const projectId = this.currentProjectId;
      const data = await this.authFetch(this.topologyUrl("/design"));
      if (projectId === this.currentProjectId) this.design = data;
    },

    async addDesign(kind) {
      this.designError = "";
      const form = { ...this.designForms[kind] };
      const numeric = ["device_id", "vlan_id", "prefix_length", "process_id"];
      for (const key of Object.keys(form)) {
        if (numeric.includes(key)) form[key] = form[key] === "" ? null : Number(form[key]);
        else if (form[key] === "") delete form[key];
      }
      try {
        await this.authFetch(this.topologyUrl(`/design/${kind}`), {
          method: "POST",
          body: JSON.stringify(form),
        });
        this.designForms[kind] = EMPTY_DESIGN_FORMS()[kind];
        this.plan = null;
        await this.loadDesign();
      } catch (err) {
        this.designError = this.describeError(err);
      }
    },

    async deleteDesign(kind, id) {
      this.designError = "";
      try {
        await this.authFetch(this.topologyUrl(`/design/${kind}/${id}`), { method: "DELETE" });
        this.plan = null;
        await this.loadDesign();
      } catch (err) {
        const used = err.data && err.data.details && err.data.details.used_by;
        this.designError = used ? `${err.message} ${used.join("; ")}` : this.describeError(err);
      }
    },

    // ------------------------------------------------------------------
    // PNETLab import
    // ------------------------------------------------------------------

    openPnet() {
      this.pnet = {
        form: { url: "", username: "", password: "", lab: "", verify_tls: true, ssh_user: "", ssh_pass: "" },
        nodes: [],
        links: [],
        loading: false,
        loaded: false,
        error: "",
        result: null,
      };
    },

    closePnet() {
      this.pnet = null;
    },

    _pnetSource() {
      const f = this.pnet.form;
      return { url: f.url, username: f.username, password: f.password, lab: f.lab, verify_tls: !!f.verify_tls };
    },

    async pnetPreview() {
      const pnet = this.pnet;
      pnet.error = "";
      pnet.result = null;
      pnet.loading = true;
      try {
        const data = await this.authFetch(this.topologyUrl("/import/pnetlab/preview"), {
          method: "POST",
          body: JSON.stringify(this._pnetSource()),
        });
        pnet.nodes = data.nodes.map((n) => ({ ...n, selected: n.supported && !n.exists }));
        pnet.links = data.links;
        pnet.loaded = true;
      } catch (err) {
        pnet.error = this.describeError(err);
      } finally {
        pnet.loading = false;
      }
    },

    get pnetSelectedCount() {
      return this.pnet ? this.pnet.nodes.filter((n) => n.selected).length : 0;
    },

    async pnetImport() {
      const pnet = this.pnet;
      pnet.error = "";
      pnet.loading = true;
      const body = {
        ...this._pnetSource(),
        nodes: pnet.nodes
          .filter((n) => n.selected)
          .map((n) => ({ id: n.id, hostname: n.hostname, management_ip: n.management_ip || null })),
      };
      if (pnet.form.ssh_user && pnet.form.ssh_pass) {
        body.credential = { username: pnet.form.ssh_user, password: pnet.form.ssh_pass };
      }
      try {
        pnet.result = await this.authFetch(this.topologyUrl("/import/pnetlab"), {
          method: "POST",
          body: JSON.stringify(body),
        });
        await this.loadTopology();
        await this.loadProjects();
      } catch (err) {
        pnet.error = this.describeError(err);
      } finally {
        pnet.loading = false;
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
      // A comparison refers to link ids; once the cabling changes it is stale.
      const signature = data.links.map((l) => l.id).join(",");
      if (this._linkSignature !== undefined && signature !== this._linkSignature) {
        this.discovery = null;
      }
      this._linkSignature = signature;
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
        const drift = this.driftOf(link.id);
        parts.push(
          `<g class="topo-link${selected ? " selected" : ""}" data-link-id="${link.id}"${drift ? ` data-drift="${drift}"` : ""}>` +
            `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="transparent" stroke-width="16" />` +
            `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${color}" stroke-width="${selected ? 4 : 2}"${dash} />` +
            this._linkLabels(link, x1, y1, x2, y2, color) +
            `</g>`
        );
      }

      for (const device of this.devices) {
        const p = pos[device.id];
        const selected =
          device.id === this.selectedNodeId ||
          device.id === this.linkSourceId ||
          this.multi.includes(device.id);
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
      if (this.band) {
        const b = this.band;
        parts.push(
          `<rect class="band" x="${Math.min(b.x1, b.x2)}" y="${Math.min(b.y1, b.y2)}" ` +
            `width="${Math.abs(b.x2 - b.x1)}" height="${Math.abs(b.y2 - b.y1)}" />`
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
        if ((event.shiftKey || event.ctrlKey || event.metaKey) && !this.linkMode) {
          // Add to / remove from the selection; no drag.
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
          if (target) origins[other] = this.nodePos(target);
        }
        this._drag = {
          id,
          origins,
          startPoint: start,
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
        this.multi = [];
        this.selectLink(Number(linkEl.dataset.linkId));
      } else {
        this._startBand(event);
      }
    },

    // Rubber band: drag on empty canvas to select the devices inside it.
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
      this.band = { x1: start.x, y1: start.y, x2: start.x, y2: start.y };
      const move = (e) => {
        const point = this._svgPoint(e);
        this.band = { ...this.band, x2: point.x, y2: point.y };
        const [left, right] = [Math.min(this.band.x1, this.band.x2), Math.max(this.band.x1, this.band.x2)];
        const [top, bottom] = [Math.min(this.band.y1, this.band.y2), Math.max(this.band.y1, this.band.y2)];
        const inside = this.devices
          .filter((d) => {
            const p = this.nodePos(d);
            return p.x >= left && p.x <= right && p.y >= top && p.y <= bottom;
          })
          .map((d) => d.id);
        this.multi = [...new Set([...base, ...inside])];
        this.selectedNodeId = this.multi.length === 1 ? this.multi[0] : null;
      };
      const up = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        this.band = null;
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
      event.preventDefault();
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
      if (!this.canEdit || this.linkMode) return;
      drag.moved = true;
      const point = this._svgPoint(event);
      const dx = point.x - drag.startPoint.x;
      const dy = point.y - drag.startPoint.y;
      for (const [id, origin] of Object.entries(drag.origins)) {
        const device = this.deviceById(id);
        if (!device) continue;
        device.pos_x = Math.max(NODE_W / 2, Math.min(4000, origin.x + dx));
        device.pos_y = Math.max(NODE_H / 2, Math.min(4000, origin.y + dy));
      }
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
      const before = drag.origins;
      const after = {};
      for (const id of Object.keys(before)) {
        const moved = this.deviceById(id);
        if (moved) after[id] = { x: moved.pos_x, y: moved.pos_y };
      }
      await this._savePositions(before, after, "Di chuyển");
    },

    // Persist positions and remember how to put them back.
    async _savePositions(before, after, label) {
      try {
        await this._putPositions(after);
        this._pushUndo({
          label,
          undo: () => this._putPositions(before, true),
          redo: () => this._putPositions(after, true),
        });
      } catch (err) {
        this.topologyError = this.describeError(err);
      }
    },

    async _putPositions(map, applyLocally = false) {
      const positions = Object.entries(map).map(([id, p]) => ({
        device_id: Number(id), x: p.x, y: p.y,
      }));
      if (applyLocally) {
        for (const item of positions) {
          const device = this.deviceById(item.device_id);
          if (device) {
            device.pos_x = item.x;
            device.pos_y = item.y;
          }
        }
      }
      await this.authFetch(this.topologyUrl("/layout"), {
        method: "PUT",
        body: JSON.stringify({ positions }),
      });
    },

    // ---- history: undo / redo -------------------------------------------

    _pushUndo(entry) {
      this.undoStack.push(entry);
      if (this.undoStack.length > 50) this.undoStack.shift();
      this.redoStack = [];
    },

    get canUndo() {
      return this.undoStack.length > 0 && !this.historyBusy;
    },

    get canRedo() {
      return this.redoStack.length > 0 && !this.historyBusy;
    },

    async undo() {
      const entry = this.undoStack[this.undoStack.length - 1];
      if (!entry || this.historyBusy) return;
      await this._runHistory(entry, "undo", () => {
        this.undoStack.pop();
        this.redoStack.push(entry);
      });
    },

    async redo() {
      const entry = this.redoStack[this.redoStack.length - 1];
      if (!entry || this.historyBusy) return;
      await this._runHistory(entry, "redo", () => {
        this.redoStack.pop();
        this.undoStack.push(entry);
      });
    },

    async _runHistory(entry, direction, moveEntry) {
      this.historyBusy = true;
      this.topologyError = "";
      try {
        await entry[direction]();
        moveEntry();
        this.plan = null;
      } catch (err) {
        this.topologyError = `Không ${direction === "undo" ? "hoàn tác" : "làm lại"} được "${entry.label}": ${this.describeError(err)}`;
      } finally {
        await this.loadTopology().catch(() => {});
        this.multi = this.multi.filter((id) => this.deviceById(id));
        this.historyBusy = false;
      }
    },

    // ---- alignment of the selected devices --------------------------------

    get canAlign() {
      return this.canEdit && this.multi.length >= 2;
    },

    async align(mode) {
      const devices = this.multi.map((id) => this.deviceById(id)).filter(Boolean);
      if (devices.length < 2) return;
      const before = {};
      const points = devices.map((d) => {
        const p = this.nodePos(d);
        before[d.id] = { ...p };
        return { id: d.id, ...p };
      });
      const xs = points.map((p) => p.x);
      const ys = points.map((p) => p.y);
      const after = {};
      const mean = (values) => values.reduce((a, b) => a + b, 0) / values.length;
      if (mode === "left") points.forEach((p) => (after[p.id] = { x: Math.min(...xs), y: p.y }));
      else if (mode === "top") points.forEach((p) => (after[p.id] = { x: p.x, y: Math.min(...ys) }));
      else if (mode === "middle") points.forEach((p) => (after[p.id] = { x: p.x, y: mean(ys) }));
      else if (mode === "center") points.forEach((p) => (after[p.id] = { x: mean(xs), y: p.y }));
      else if (mode === "spread-x" || mode === "spread-y") {
        const horizontal = mode === "spread-x";
        const sorted = [...points].sort((a, b) => (horizontal ? a.x - b.x : a.y - b.y));
        const first = horizontal ? sorted[0].x : sorted[0].y;
        const last = horizontal ? sorted[sorted.length - 1].x : sorted[sorted.length - 1].y;
        const step = sorted.length > 1 ? (last - first) / (sorted.length - 1) : 0;
        sorted.forEach((p, i) => {
          after[p.id] = horizontal ? { x: first + step * i, y: p.y } : { x: p.x, y: first + step * i };
        });
      } else return;
      for (const [id, p] of Object.entries(after)) {
        const device = this.deviceById(id);
        device.pos_x = p.x;
        device.pos_y = p.y;
      }
      await this._savePositions(before, after, "Căn chỉnh");
    },

    async deleteSelectedDevices() {
      const devices = this.multi.map((id) => this.deviceById(id)).filter(Boolean);
      if (!devices.length) return;
      const names = devices.map((d) => d.hostname).join(", ");
      if (!window.confirm(`Xoá ${devices.length} thiết bị (${names}) và các liên kết của chúng?\nKhông thể hoàn tác.`)) return;
      this.topologyError = "";
      try {
        for (const device of devices) {
          await this.authFetch(`/api/devices/${device.id}`, { method: "DELETE" });
        }
      } catch (err) {
        this.topologyError = this.describeError(err);
      }
      this.multi = [];
      this.selectedNodeId = null;
      this.undoStack = [];
      this.redoStack = [];
      await this.loadTopology();
      await this.loadProjects();
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
      this.multi = [device.id];
    },

    isAsa(deviceId) {
      const device = this.deviceById(deviceId);
      return !!device && device.device_type === "cisco_asa";
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

    // What it takes to recreate a link exactly as it was.
    _linkCreateBody(link) {
      return {
        device_a_id: link.device_a_id,
        interface_a: link.interface_a,
        device_b_id: link.device_b_id,
        interface_b: link.interface_b,
        link_type: link.link_type,
        network: link.network,
        ip_a: link.ip_a,
        ip_b: link.ip_b,
        allowed_vlans: link.allowed_vlans,
        bring_up: link.bring_up,
        description: link.description,
        nameif_a: link.nameif_a,
        security_a: link.security_a,
        nameif_b: link.nameif_b,
        security_b: link.security_b,
      };
    },

    _linkEditBody(link) {
      const { device_a_id, interface_a, device_b_id, interface_b, ...editable } = this._linkCreateBody(link);
      return editable;
    },

    // Undo/redo for a link that was just created: delete it, or create it again.
    _rememberLinkCreated(link) {
      const body = this._linkCreateBody(link);
      const ref = { id: link.id };
      this._pushUndo({
        label: "Tạo dây nối",
        undo: () => this.authFetch(this.topologyUrl(`/links/${ref.id}`), { method: "DELETE" }),
        redo: async () => {
          const again = await this.authFetch(this.topologyUrl("/links"), {
            method: "POST",
            body: JSON.stringify(body),
          });
          ref.id = again.id;
        },
      });
    },

    async quickLink(sourceId, targetId) {
      this.topologyError = "";
      try {
        const link = await this.authFetch(this.topologyUrl("/links"), {
          method: "POST",
          body: JSON.stringify({ device_a_id: sourceId, device_b_id: targetId }),
        });
        this._rememberLinkCreated(link);
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

    // A device that was just dropped can be removed again (and dropped again).
    // Deleting an existing device is not undoable: its SSH login is gone.
    _rememberDeviceAdded(device, key, point) {
      const ref = { id: device.id };
      this._pushUndo({
        label: "Thêm thiết bị",
        undo: () => this.authFetch(`/api/devices/${ref.id}`, { method: "DELETE" }),
        redo: async () => {
          const body = { template: key };
          if (point) {
            body.x = Math.max(NODE_W / 2, Math.round(point.x));
            body.y = Math.max(NODE_H / 2, Math.round(point.y));
          }
          if (this.defaultSsh.username && this.defaultSsh.password) body.credential = { ...this.defaultSsh };
          const again = await this.authFetch(this.topologyUrl("/quick-device"), {
            method: "POST",
            body: JSON.stringify(body),
          });
          ref.id = again.id;
        },
      });
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
        this.multi = [device.id];
        this._rememberDeviceAdded(device, key, point);
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
        nameif_a: link.nameif_a || "",
        security_a: link.security_a === null || link.security_a === undefined ? "" : link.security_a,
        nameif_b: link.nameif_b || "",
        security_b: link.security_b === null || link.security_b === undefined ? "" : link.security_b,
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
      // Firewall interface names / trust levels (only meaningful for an ASA end).
      for (const side of ["a", "b"]) {
        payload[`nameif_${side}`] = String(form[`nameif_${side}`] || "").trim() || null;
        const level = form[`security_${side}`];
        payload[`security_${side}`] = level === "" || level === null ? null : Number(level);
      }
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
          const old = this.links.find((l) => l.id === form.id);
          const oldBody = old ? this._linkEditBody(old) : null;
          const updated = await this.authFetch(this.topologyUrl(`/links/${form.id}`), {
            method: "PUT",
            body: JSON.stringify(this._linkPayload(false)),
          });
          if (oldBody) {
            const newBody = this._linkEditBody(updated);
            const id = form.id;
            this._pushUndo({
              label: "Sửa dây nối",
              undo: () => this.authFetch(this.topologyUrl(`/links/${id}`), { method: "PUT", body: JSON.stringify(oldBody) }),
              redo: () => this.authFetch(this.topologyUrl(`/links/${id}`), { method: "PUT", body: JSON.stringify(newBody) }),
            });
          }
        } else {
          if (!form.device_a_id || !form.device_b_id) {
            this.topologyError = "Chọn hai thiết bị cần nối.";
            return;
          }
          const created = await this.authFetch(this.topologyUrl("/links"), {
            method: "POST",
            body: JSON.stringify(this._linkPayload(true)),
          });
          this._rememberLinkCreated(created);
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
      const old = this.links.find((l) => l.id === id);
      try {
        await this.authFetch(this.topologyUrl(`/links/${id}`), { method: "DELETE" });
        if (old) {
          const body = this._linkCreateBody(old);
          const ref = { id };
          this._pushUndo({
            label: "Xoá dây nối",
            undo: async () => {
              const again = await this.authFetch(this.topologyUrl("/links"), {
                method: "POST",
                body: JSON.stringify(body),
              });
              ref.id = again.id;
            },
            redo: () => this.authFetch(this.topologyUrl(`/links/${ref.id}`), { method: "DELETE" }),
          });
        }
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
      const before = {};
      const after = {};
      this.devices.forEach((device, index) => {
        before[device.id] = { ...this.nodePos(device) };
        after[device.id] = { x: 100 + (index % columns) * 190, y: 90 + Math.floor(index / columns) * 150 };
        device.pos_x = after[device.id].x;
        device.pos_y = after[device.id].y;
      });
      await this._savePositions(before, after, "Sắp xếp lại");
    },

    // ---- discovery: compare the diagram with what the devices report -----

    driftOf(linkId) {
      const result = this.discovery && this.discovery.result;
      if (!result) return "";
      if (result.matched.includes(linkId)) return "matched";
      if (result.missing.includes(linkId)) return "missing";
      if (result.unknown.includes(linkId)) return "unknown";
      return "";
    },

    async runDiscovery() {
      this.topologyError = "";
      this.discovery = { loading: true, error: "", result: null };
      try {
        const result = await this.authFetch(this.topologyUrl("/discover"), {
          method: "POST",
          body: "{}",
        });
        this.discovery = { loading: false, error: "", result };
      } catch (err) {
        this.discovery = { loading: false, error: this.describeError(err), result: null };
      }
    },

    clearDiscovery() {
      this.discovery = null;
    },

    async addDiscovered(links) {
      this.topologyError = "";
      try {
        const done = await this.authFetch(this.topologyUrl("/discover/apply"), {
          method: "POST",
          body: JSON.stringify({ links }),
        });
        await this.loadTopology();
        if (done.skipped.length) {
          this.topologyError = done.skipped.map((s) => s.message).join("; ");
        }
        await this.runDiscovery();
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
