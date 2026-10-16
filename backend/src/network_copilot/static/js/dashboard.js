function storedUser() {
  try {
    const user = JSON.parse(localStorage.getItem("nc_user") || "null");
    return user && typeof user === "object" ? user : null;
  } catch {
    localStorage.removeItem("nc_user");
    return null;
  }
}

document.addEventListener("alpine:init", () => {
  Alpine.data("dashboardApp", () => ({
    token: localStorage.getItem("nc_token") || null,
    currentUser: storedUser(),
    loginForm: { username: "", password: "" },
    loginError: "",
    _sessionGeneration: 0,

    projectName: "",
    projects: [],
    currentProjectId: Number(localStorage.getItem("nc_project_id")) || null,
    noProject: false,
    summary: null,
    lastError: "",
    _summaryTimer: null,
    _summaryGeneration: 0,

    init() {
      if (!this.token) return;
      return this.startApp();
    },

    async authFetch(path, options = {}) {
      const token = this.token;
      const generation = this._sessionGeneration;
      const projectId = localStorage.getItem("nc_project_id");
      const headers = Object.assign(
        { "Content-Type": "application/json" },
        projectId ? { "X-Project-Id": projectId } : {},
        options.headers || {},
        token ? { Authorization: `Bearer ${token}` } : {}
      );
      const response = await fetch(path, { ...options, headers });
      const data = await response.json().catch(() => ({}));
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
        throw error;
      }
      return data;
    },

    async login() {
      this.loginError = "";
      const generation = this._sessionGeneration;
      try {
        const response = await fetch("/api/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(this.loginForm),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.message || "Đăng nhập thất bại.");
        if (generation !== this._sessionGeneration) return;
        this.token = data.access_token;
        this.currentUser = data.user;
        localStorage.setItem("nc_token", this.token);
        localStorage.setItem("nc_user", JSON.stringify(this.currentUser));
        if (data.mfa_setup_required) {
          window.location.assign("/");
          return;
        }
        this.loginForm = { username: "", password: "" };
        await this.startApp();
      } catch (err) {
        this.loginError = err.message || "Đăng nhập thất bại.";
      }
    },

    logout() {
      this._sessionGeneration += 1;
      this._summaryGeneration += 1;
      this.token = null;
      this.currentUser = null;
      localStorage.removeItem("nc_token");
      localStorage.removeItem("nc_user");
      this.stopPolling();
      this.summary = null;
      this.projectName = "";
      this.lastError = "";
    },

    async loadProjectName() {
      const data = await this.authFetch("/api/projects");
      this.projects = data.items;
      const stored = Number(localStorage.getItem("nc_project_id"));
      const project =
        data.items.find((item) => item.id === stored) ||
        data.items[0] || null;
      this.projectName = project ? project.name : "";
      this.currentProjectId = project ? project.id : null;
      this.noProject = !project;
      if (project) localStorage.setItem("nc_project_id", String(project.id));
      else localStorage.removeItem("nc_project_id");
    },

    async selectProject(projectId) {
      const project = this.projects.find((item) => item.id === Number(projectId));
      if (!project) return;
      this._summaryGeneration += 1;
      this.currentProjectId = project.id;
      this.projectName = project.name;
      this.summary = null;
      localStorage.setItem("nc_project_id", String(project.id));
      await this.refreshSummary().catch(() => {});
    },

    async startApp() {
      const generation = this._sessionGeneration;
      await this.loadProjectName().catch(() => {});
      if (generation !== this._sessionGeneration || this.noProject) return;
      await this.refreshSummary().catch(() => {});
      if (generation !== this._sessionGeneration) return;
      this.startPolling();
    },

    startPolling() {
      this.stopPolling();
      this._summaryTimer = setInterval(() => {
        this.refreshSummary().catch(() => {});
      }, 10000);
    },

    stopPolling() {
      clearInterval(this._summaryTimer);
    },

    async refreshSummary() {
      const generation = this._summaryGeneration;
      try {
        const data = await this.authFetch("/api/dashboard/summary");
        if (generation === this._summaryGeneration) {
          this.summary = data;
          this.lastError = "";
        }
      } catch (err) {
        if (generation === this._summaryGeneration && this.currentUser) {
          this.lastError = new Date().toLocaleTimeString("vi-VN");
        }
        throw err;
      }
    },

    allOspfNoData() {
      return (
        this.summary &&
        this.summary.ospf.length > 0 &&
        this.summary.ospf.every((entry) => entry.health === "no_data")
      );
    },
  }));
});
