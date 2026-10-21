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
  pc:
    '<rect x="4" y="4" width="16" height="11" rx="1.5" /><path d="M9 20h6M12 15v5" />',
  server:
    '<rect x="4" y="3" width="16" height="7" rx="1.5" /><rect x="4" y="13" width="16" height="7" rx="1.5" /><path d="M8 6.5h.01M8 16.5h.01M12 6.5h5M12 16.5h5" />',
  cloud:
    '<path d="M7 18a4 4 0 0 1-.6-7.95A5.5 5.5 0 0 1 17 9a4.5 4.5 0 0 1 .5 9z" />',
  ap:
    '<circle cx="12" cy="16" r="2" /><path d="M7.5 11.5a6.4 6.4 0 0 1 9 0M4.5 8.5a10.6 10.6 0 0 1 15 0" />',
  segment:
    '<path d="M3 12h18" /><circle cx="6" cy="12" r="1.5" /><circle cx="12" cy="12" r="1.5" /><circle cx="18" cy="12" r="1.5" /><path d="M6 12V7M12 12v5M18 12V7" />',
};

function templateKeyFor(device) {
  if (["pc", "server", "cloud", "ap", "segment"].includes(device.kind)) return device.kind;
  if (device.device_type === "cisco_asa" || device.role === "firewall") return "firewall";
  if (device.role === "core") return "router";
  if (device.role === "distribution") return "switch_l3";
  return "switch";
}

const LINK_COLORS = { physical: "#75899f", routed: "#22c55e", trunk: "#06b6d4" };

// The canvas module (canvas.js) draws nodes and links with these.
window.NCShared = { ICONS, LINK_COLORS, templateKeyFor, esc };

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

const EMPTY_DESIGN = () => ({ vlans: [], access_ports: [], svis: [], loopbacks: [], subinterfaces: [], routes: [], ospf: [] });

const EMPTY_DESIGN_FORMS = () => ({
  vlans: { vlan_id: "", name: "" },
  "access-ports": { device_id: "", interface: "", vlan_id: "" },
  svis: { device_id: "", vlan_id: "", ip: "", prefix_length: 24 },
  loopbacks: { device_id: "", number: 0, ip: "" },
  subinterfaces: { device_id: "", interface: "", vlan_id: "", ip: "", prefix_length: 24 },
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
  channel_id: "",
  bring_up: true,
  description: "",
  nameif_a: "",
  security_a: "",
  nameif_b: "",
  security_b: "",
});

// Copy a mixin's members onto the component *with their getters intact*;
// object spread would call each getter once and keep only its value.
function mix(base, mixin) {
  return Object.defineProperties(base, Object.getOwnPropertyDescriptors(mixin));
}

document.addEventListener("alpine:init", () => {
  Alpine.data("projectsApp", () => mix({

    // -- auth --
    token: localStorage.getItem("nc_token") || null,
    currentUser: storedUser(),
    loginForm: { username: "", password: "" },
    loginError: "",
    _sessionGeneration: 0,

    // -- projects --
    projects: [],
    currentProjectId: (() => {
      const requested = new URLSearchParams(window.location.search).get("project_id");
      if (requested && Number.isInteger(Number(requested))) return Number(requested);
      const stored = localStorage.getItem("nc_project_id");
      return stored ? Number(stored) : null;
    })(),
    chatSessionId: (() => {
      const params = new URLSearchParams(window.location.search);
      const requested = Number(params.get("session_id"));
      const stored = Number(localStorage.getItem("nc_session_id"));
      return Number.isInteger(requested) && requested > 0
        ? requested
        : Number.isInteger(stored) && stored > 0 ? stored : null;
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
    aiSuggestion: null,
    aiSuggestionLoading: false,
    aiSuggestionError: "",
    topologyView: "ai",
    tab: new URLSearchParams(window.location.search).get("tab") === "topology" ? "topology" : "devices",

    // -- devices --
    devices: [],
    deviceForm: EMPTY_DEVICE_FORM(),
    deviceFormOpen: false,
    deviceError: "",
    deviceNotice: "",
    deviceBusy: {},
    baselineBusy: {},

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
    topologyNotice: "",
    discovery: null,
    multi: [],
    props: null,
    propsError: "",
    clipboard: [],
    pasteCount: 0,
    portsByLink: { a: [], b: [] },
    undoStack: [],
    redoStack: [],
    historyBusy: false,
    deviceUndoable: false,
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
      this.initCanvas();
      this.initDesignAids();
      window.addEventListener("keydown", (event) => this.onKeyDown(event));
      this.$watch("selectedNodeId", () => this.syncProps());
      // The canvas can only be measured once its tab is showing.
      this.$watch("tab", (value) => {
        if (value !== "topology") return;
        this.$nextTick(() => {
          if (!this._fittedVisible && this.devices.length) {
            this._fittedVisible = true;
            this.fitView();
          }
        });
      });
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
      if (this.onCanvasKey(event)) return;
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
            addressing_pool: project.addressing_pool || "",
            ai_management_enabled: Boolean(project.ai_management_enabled),
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
      this.resetDesignAids();
      this.design = EMPTY_DESIGN();
      this.designError = "";
      this.undoStack = [];
      this.redoStack = [];
      this.multi = [];
      this._fitted = false;
      this._fittedVisible = false;
      this.props = null;
      this.menu = null;
      this.discovery = null;
      this.linkFormOpen = false;
      this.deviceFormOpen = false;
      this.deviceError = "";
      this.deviceNotice = "";
      this.deviceUndoable = false;
      this.topologyError = "";
      this.aiSuggestion = null;
      this.aiSuggestionError = "";
      this.aiSuggestionLoading = false;
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
      const aiSuggestionTask = this.loadAiSuggestion();
      await this.loadDevices();
      if (projectId !== this.currentProjectId) return;
      await this.loadTopology();
      if (projectId !== this.currentProjectId) return;
      await this.loadTemplates();
      if (projectId !== this.currentProjectId) return;
      await this.loadDesign();
      if (projectId !== this.currentProjectId) return;
      await this.loadMembers();
      if (projectId !== this.currentProjectId) return;
      await aiSuggestionTask;
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
      if (
        this.settingsForm.ai_management_enabled &&
        this.project && !this.project.ai_management_enabled &&
        !window.confirm("Bật AI Management cho phép thành viên duyệt từng gợi ý cấu hình trong chat để gửi tới thiết bị PNETLab đã cô lập. Tiếp tục bật tính năng này?")
      ) return;
      try {
        await this.authFetch(`/api/projects/${this.currentProjectId}`, {
          method: "PUT",
          body: JSON.stringify({
            name: this.settingsForm.name,
            description: this.settingsForm.description || null,
            management_network: this.settingsForm.management_network,
            environment: this.settingsForm.environment,
            addressing_pool: this.settingsForm.addressing_pool || null,
            ai_management_enabled: Boolean(this.settingsForm.ai_management_enabled),
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
      // Same order as the diagram: devices never placed take their grid slot
      // from it, so another order would make them jump around.
      if (projectId === this.currentProjectId) {
        this.devices = data.items.sort((a, b) => window.NCLayout.naturalCompare(a.hostname, b.hostname));
      }
    },

    openDeviceForm(device) {
      this.deviceError = "";
      this.deviceNotice = "";
      this.deviceUndoable = false;
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
      this.deviceUndoable = false;
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
      this.deviceUndoable = false;
      try {
        const result = await this.authFetch(`/api/devices/${device.id}?undo=1`, { method: "DELETE" });
        this._rememberDevicesDeleted([{ id: device.id, token: result.restore_token }], `Xoá ${device.hostname}`);
        this.multi = [];
        await this.loadDevices();
        await this.loadTopology();
        await this.loadProjects();
        this.deviceNotice = `Đã xoá ${device.hostname}.`;
        this.deviceUndoable = true;
      } catch (err) {
        this.deviceError = this.describeError(err);
      }
    },

    async testConnection(device) {
      this.deviceError = "";
      this.deviceNotice = "";
      this.deviceUndoable = false;
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
      this.deviceUndoable = false;
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
      const numeric = ["device_id", "vlan_id", "prefix_length", "process_id", "number"];
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

    networkDiagram(diagram) {
      return window.NCDiagram ? window.NCDiagram.render(diagram) : "";
    },

    async loadAiSuggestion() {
      const projectId = this.currentProjectId;
      if (!projectId) {
        this.aiSuggestion = null;
        return;
      }
      this.aiSuggestionLoading = true;
      this.aiSuggestionError = "";
      try {
        const params = this.chatSessionId ? `?session_id=${this.chatSessionId}` : "";
        const data = await this.authFetch(this.topologyUrl(`/ai-suggestion${params}`));
        if (projectId === this.currentProjectId) this.aiSuggestion = data.suggestion || null;
      } catch (err) {
        if (projectId === this.currentProjectId) {
          this.aiSuggestion = null;
          this.aiSuggestionError = this.describeError(err);
        }
      } finally {
        if (projectId === this.currentProjectId) this.aiSuggestionLoading = false;
      }
    },

    async loadTopology() {
      const projectId = this.currentProjectId;
      const data = await this.authFetch(this.topologyUrl());
      if (projectId !== this.currentProjectId) return;
      this.devices = data.nodes.sort((a, b) => window.NCLayout.naturalCompare(a.hostname, b.hostname));
      this.links = data.links;
      this.notes = data.notes || [];
      if (this.selectedDecoId && !this.notes.some((n) => n.id === this.selectedDecoId)) this.selectedDecoId = null;
      if (!this._fitted && this.devices.length) {
        this._fitted = true;
        this.$nextTick(() => this.fitView());
      }
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

    _linkLabels(link, x1, y1, x2, y2, color) {
      const at = (t) => ({ x: x1 + (x2 - x1) * t, y: y1 + (y2 - y1) * t });
      const near = at(0.24);
      const far = at(0.76);
      const mid = at(0.5);
      let middle = "";
      if (link.link_type === "routed" && link.network) middle = link.network;
      else if (link.link_type === "trunk") {
        middle = (link.allowed_vlans ? `trunk ${link.allowed_vlans}` : "trunk") + (link.channel_id ? ` · Po${link.channel_id}` : "");
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

    // ---- properties panel, context menu, copy & paste, port lists ----------

    syncProps() {
      const device = this.selectedNode;
      this.propsError = "";
      this.props = device && this.multi.length <= 1
        ? {
            id: device.id,
            hostname: device.hostname,
            management_ip: device.management_ip || "",
            role: device.role,
            kind: device.kind,
            managed: device.managed !== false,
            lifecycle: device.lifecycle,
            data_ip: device.data_ip || "",
            data_gateway: device.data_gateway || "",
            environment: device.environment,
            isolated: Boolean(device.isolated),
            ssh_port: device.ssh_port,
            monitoring_enabled: device.monitoring_enabled,
            description: device.description || "",
          }
        : null;
    },

    async saveProps() {
      if (!this.props) return;
      this.propsError = "";
      const p = this.props;
      try {
        const body = {
          hostname: p.hostname.trim(),
          environment: p.environment,
          description: p.description || null,
          data_ip: p.data_ip || null,
          data_gateway: p.data_gateway || null,
          isolated: Boolean(p.isolated),
        };
        if (p.managed) {
          body.role = p.role;
          body.ssh_port = Number(p.ssh_port) || 22;
          body.monitoring_enabled = !!p.monitoring_enabled;
          if (p.lifecycle !== "planned" || p.management_ip) body.management_ip = p.management_ip;
        }
        const updated = await this.authFetch(`/api/devices/${p.id}`, {
          method: "PUT",
          body: JSON.stringify(body),
        });
        await this.loadTopology();
        await this.loadDevices();
        this.selectedNodeId = updated.id;
        this.syncProps();
      } catch (err) {
        this.propsError = this.describeError(err);
      }
    },

    async captureBaseline(deviceId) {
      if (this.baselineBusy[deviceId]) return;
      this.deviceError = "";
      this.deviceNotice = "";
      this.deviceUndoable = false;
      this.baselineBusy[deviceId] = true;
      try {
        await this.authFetch(`/api/governance/devices/${deviceId}/baseline`, {
          method: "POST",
        });
        this.deviceNotice = "Đã lưu cấu hình nền cho thiết bị. AI Management có thể dùng nó để kiểm tra trước khi áp dụng.";
      } catch (err) {
        this.deviceError = this.describeError(err);
      } finally {
        delete this.baselineBusy[deviceId];
      }
    },

    onCanvasDblClick(event) {
      const nodeEl = event.target.closest("[data-node-id]");
      if (!nodeEl || !this.canEdit) return;
      const device = this.deviceById(Number(nodeEl.dataset.nodeId));
      if (!device) return;
      this.multi = [device.id];
      this.selectedLinkId = null;
      this.selectedNodeId = device.id;
      this.syncProps();
      this.$nextTick(() => {
        const input = this.$refs.propHostname;
        if (input) { input.focus(); input.select(); }
      });
    },

    onContextMenu(event) {
      event.preventDefault();
      const nodeEl = event.target.closest("[data-node-id]");
      const linkEl = event.target.closest("[data-link-id]");
      if (nodeEl) {
        const id = Number(nodeEl.dataset.nodeId);
        if (!this.multi.includes(id)) {
          this.multi = [id];
          this.selectedNodeId = id;
          this.selectedLinkId = null;
        }
        this.menu = { x: event.clientX, y: event.clientY, kind: "node", id };
      } else if (linkEl) {
        const id = Number(linkEl.dataset.linkId);
        this.selectLink(id);
        this.menu = { x: event.clientX, y: event.clientY, kind: "link", id };
      } else {
        this.menu = { x: event.clientX, y: event.clientY, kind: "canvas" };
      }
    },

    menuAction(action) {
      const menu = this.menu;
      this.menu = null;
      if (!menu) return;
      if (action === "edit") this.onCanvasDblClick({ target: { closest: () => ({ dataset: { nodeId: menu.id } }) } });
      else if (action === "duplicate") this.duplicateSelection();
      else if (action === "link-from") {
        this.toggleLinkMode();
        this.linkSourceId = menu.id;
      } else if (action === "delete-node") this.multi.length > 1 ? this.deleteSelectedDevices() : this.deleteDevice(this.deviceById(menu.id));
      else if (action === "delete-link") {
        this.linkForm.id = menu.id;
        this.deleteLink();
      } else if (action === "paste") this.pasteClipboard();
      else if (action === "fit") this.fitView();
      else if (action === "select-all") this.multi = this.devices.map((d) => d.id);
    },

    copySelection() {
      if (!this.multi.length) return;
      this.clipboard = [...this.multi];
      this.pasteCount = 0;
      this.topologyNotice = `Đã sao chép ${this.clipboard.length} thiết bị. Ctrl+V để dán.`;
    },

    async pasteClipboard() {
      if (!this.clipboard.length || !this.canEdit) return;
      this.pasteCount += 1;
      await this._duplicate(this.clipboard, 40 * this.pasteCount, 40 * this.pasteCount);
    },

    async duplicateSelection() {
      if (!this.multi.length || !this.canEdit) return;
      await this._duplicate([...this.multi], 50, 50);
    },

    async _duplicate(ids, dx, dy) {
      this.topologyError = "";
      try {
        const result = await this.authFetch(this.topologyUrl("/duplicate"), {
          method: "POST",
          body: JSON.stringify({ device_ids: ids, dx, dy }),
        });
        const ref = { ids: result.devices.map((d) => d.id) };
        this._pushUndo({
          label: "Nhân bản thiết bị",
          undo: async () => {
            for (const id of ref.ids) await this.authFetch(`/api/devices/${id}`, { method: "DELETE" });
          },
          redo: async () => {
            const again = await this.authFetch(this.topologyUrl("/duplicate"), {
              method: "POST",
              body: JSON.stringify({ device_ids: ids, dx, dy }),
            });
            ref.ids = again.devices.map((d) => d.id);
          },
        });
        await this.loadTopology();
        await this.loadProjects();
        this.multi = ref.ids;
        this.selectedNodeId = ref.ids.length === 1 ? ref.ids[0] : null;
        if (result.notes && result.notes.length) this.topologyNotice = result.notes.join(" ");
        else this.topologyNotice = "Đã nhân bản. Mật khẩu SSH không được sao chép: nhập lại cho bản sao.";
      } catch (err) {
        this.topologyError = this.describeError(err);
      }
    },

    // Free ports of a device, for the cable form's drop-down suggestions.
    async loadPorts(side) {
      const id = this.linkForm[`device_${side}_id`];
      if (!id) {
        this.portsByLink = { ...this.portsByLink, [side]: [] };
        return;
      }
      try {
        const data = await this.authFetch(this.topologyUrl(`/devices/${id}/ports`));
        this.portsByLink = { ...this.portsByLink, [side]: data.ports };
      } catch {
        this.portsByLink = { ...this.portsByLink, [side]: [] };
      }
    },

    // Group boxes and notes (added in the design-aids step) are drawn below the nodes.
    get decorationsSvg() {
      return this.decorationMarkup ? this.decorationMarkup() : "";
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
      if (!window.confirm(`Xoá ${devices.length} thiết bị (${names}) và các liên kết của chúng?`)) return;
      this.topologyError = "";
      const deleted = [];
      try {
        for (const device of devices) {
          const result = await this.authFetch(`/api/devices/${device.id}?undo=1`, { method: "DELETE" });
          deleted.push({ id: device.id, token: result.restore_token });
        }
      } catch (err) {
        this.topologyError = this.describeError(err);
      }
      if (deleted.length) this._rememberDevicesDeleted(deleted, `Xoá ${deleted.length} thiết bị`);
      this.multi = [];
      this.selectedNodeId = null;
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
        channel_id: link.channel_id,
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

    // Undo of a deletion restores each device from the token the server sealed
    // when deleting it, SSH login included. The last one deleted comes back
    // first, so a cable between two of them is restored with the second one.
    _rememberDevicesDeleted(items, label) {
      const refs = items.map((item) => ({ ...item }));
      const reload = async () => {
        await this.loadDevices();
        await this.loadProjects();
      };
      this._pushUndo({
        label,
        undo: async () => {
          for (const ref of [...refs].reverse()) {
            const device = await this.authFetch("/api/devices/restore", {
              method: "POST",
              body: JSON.stringify({ restore_token: ref.token }),
            });
            ref.id = device.id;
          }
          await reload();
        },
        redo: async () => {
          for (const ref of refs) {
            const result = await this.authFetch(`/api/devices/${ref.id}?undo=1`, { method: "DELETE" });
            ref.token = result.restore_token;
          }
          await reload();
        },
      });
    },

    // Read-only summary of the design, the same view the chat shows for an AI proposal.
    get designVlanSummary() {
      const d = this.design;
      const short = (name) => name.replace("GigabitEthernet", "Gi");
      return d.vlans.map((v) => ({
        vlan_id: v.vlan_id,
        name: v.name,
        gateways: [
          ...d.svis.filter((x) => x.vlan_id === v.vlan_id).map((x) => `${x.hostname} ${x.ip}/${x.prefix_length}`),
          ...d.subinterfaces.filter((x) => x.vlan_id === v.vlan_id)
            .map((x) => `${x.hostname} ${short(x.interface)}.${x.vlan_id} ${x.ip}/${x.prefix_length}`),
        ].join(", "),
        ports: d.access_ports.filter((x) => x.vlan_id === v.vlan_id).map((x) => `${x.hostname} ${short(x.interface)}`).join(", "),
      }));
    },

    get designRoutingSummary() {
      const d = this.design;
      const lines = [];
      if (d.ospf.length) lines.push(`OSPF area 0 trên ${d.ospf.map((o) => o.hostname).join(", ")}`);
      for (const r of d.routes) lines.push(`Route tĩnh ${r.hostname}: ${r.network}/${r.prefix_length} qua ${r.next_hop}`);
      for (const l of d.loopbacks) lines.push(`${l.hostname} Loopback${l.number}: ${l.ip}`);
      return lines;
    },

    openDesignChat() {
      try { localStorage.setItem("nc_design_mode", "1"); } catch { /* optional */ }
    },

    async undoDeviceDelete() {
      this.deviceUndoable = false;
      this.deviceNotice = "";
      this.deviceUndoable = false;
      await this.undo();
      if (this.topologyError) this.deviceError = this.topologyError;
      else this.deviceNotice = "Đã khôi phục thiết bị.";
    },

    // A device that was just dropped can be removed again (and dropped again).
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
          if (this.placePlanned) body.planned = true;
          else if (this.defaultSsh.username && this.defaultSsh.password) body.credential = { ...this.defaultSsh };
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
      // A click on the palette puts the device in the middle of what is on
      // screen; either way it goes to the nearest spot no other device covers.
      const v = this.view;
      point = this._freeSpot(point || { x: v.x + v.w / 2, y: v.y + v.h / 2 });
      const body = { template: key, x: point.x, y: point.y };
      if (this.placePlanned) {
        body.planned = true;
      } else if (this.defaultSsh.username && this.defaultSsh.password) {
        body.credential = { ...this.defaultSsh };
      }
      try {
        const device = await this.authFetch(this.topologyUrl("/quick-device"), {
          method: "POST",
          body: JSON.stringify(body),
        });
        await this.loadTopology();
        // Only the device count in the project list changes; no need to wait.
        this.loadProjects().catch(() => {});
        this.selectedLinkId = null;
        this.selectedNodeId = device.id;
        this.multi = [device.id];
        this._reveal(point);
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
        channel_id: link.channel_id || "",
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
      if (typed === "trunk") {
        payload.allowed_vlans = form.allowed_vlans.trim() || null;
        payload.channel_id = form.channel_id === "" || form.channel_id === null ? null : Number(form.channel_id);
      }
      // Firewall interface names / trust levels (only meaningful for an ASA end).
      for (const side of ["a", "b"]) {
        payload[`nameif_${side}`] = String(form[`nameif_${side}`] || "").trim() || null;
        const level = form[`security_${side}`];
        payload[`security_${side}`] = level === "" || level === null ? null : Number(level);
      }
      if (creating) {
        payload.device_a_id = Number(form.device_a_id);
        // Left empty, the server picks the lowest free port.
        payload.interface_a = String(form.interface_a || "").trim() || null;
        payload.device_b_id = Number(form.device_b_id);
        payload.interface_b = String(form.interface_b || "").trim() || null;
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
  }, mix(window.NCCanvas(), window.NCDesignAids())));
});
