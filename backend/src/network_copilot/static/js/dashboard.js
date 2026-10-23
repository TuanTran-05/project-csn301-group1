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
    activityEvents: [],
    activityHasMore: false,
    activityLoading: false,
    activityError: "",
    _activityExhausted: false,
    _activityGeneration: 0,

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
      this._activityGeneration += 1;
      this.token = null;
      this.currentUser = null;
      localStorage.removeItem("nc_token");
      localStorage.removeItem("nc_user");
      this.stopPolling();
      this.summary = null;
      this.activityEvents = [];
      this.activityHasMore = false;
      this.activityLoading = false;
      this.activityError = "";
      this._activityExhausted = false;
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
      this._activityGeneration += 1;
      this.currentProjectId = project.id;
      this.projectName = project.name;
      this.summary = null;
      this.activityEvents = [];
      this.activityHasMore = false;
      this.activityLoading = false;
      this.activityError = "";
      this._activityExhausted = false;
      localStorage.setItem("nc_project_id", String(project.id));
      await Promise.all([
        this.refreshSummary().catch(() => {}),
        this.refreshActivity(true).catch(() => {}),
      ]);
    },

    async startApp() {
      const generation = this._sessionGeneration;
      await this.loadProjectName().catch(() => {});
      if (generation !== this._sessionGeneration || this.noProject) return;
      await Promise.all([
        this.refreshSummary().catch(() => {}),
        this.refreshActivity(true).catch(() => {}),
      ]);
      if (generation !== this._sessionGeneration) return;
      this.startPolling();
    },

    startPolling() {
      this.stopPolling();
      this._summaryTimer = setInterval(() => {
        this.refreshSummary().catch(() => {});
        this.refreshActivity().catch(() => {});
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

    activityDate(value) {
      if (!value) return null;
      const raw = String(value);
      const iso = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw) ? raw : `${raw}Z`;
      const date = new Date(iso);
      return Number.isFinite(date.getTime()) ? date : null;
    },

    activityDateKey(value) {
      const date = this.activityDate(value);
      if (!date) return "unknown";
      const parts = new Intl.DateTimeFormat("en-CA", {
        timeZone: "Asia/Ho_Chi_Minh",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      }).formatToParts(date);
      const fields = Object.fromEntries(parts.map((part) => [part.type, part.value]));
      return `${fields.year}-${fields.month}-${fields.day}`;
    },

    formatActivityDate(value) {
      const date = this.activityDate(value);
      if (!date) return "Ngày không xác định";
      const parts = new Intl.DateTimeFormat("vi-VN", {
        timeZone: "Asia/Ho_Chi_Minh",
        year: "numeric",
        month: "numeric",
        day: "numeric",
      }).formatToParts(date);
      const fields = Object.fromEntries(parts.map((part) => [part.type, part.value]));
      return `Ngày ${fields.day} tháng ${fields.month} năm ${fields.year}`;
    },

    formatActivityTime(value) {
      const date = this.activityDate(value);
      if (!date) return "--:--:--";
      return new Intl.DateTimeFormat("vi-VN", {
        timeZone: "Asia/Ho_Chi_Minh",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      }).format(date);
    },

    compareActivityEvents(a, b) {
      const left = this.activityDate(a.created_at)?.getTime() || 0;
      const right = this.activityDate(b.created_at)?.getTime() || 0;
      return right - left || Number(b.id) - Number(a.id);
    },

    get activityGroups() {
      const groups = new Map();
      for (const event of this.activityEvents) {
        const key = this.activityDateKey(event.created_at);
        if (!groups.has(key)) {
          groups.set(key, {
            key,
            label: this.formatActivityDate(event.created_at),
            events: [],
          });
        }
        groups.get(key).events.push(event);
      }
      return [...groups.values()];
    },

    activityActionLabel(action) {
      const labels = {
        "project.create": "Tạo dự án",
        "project.update": "Cập nhật dự án",
        "project.delete": "Xóa dự án",
        "project.share": "Chia sẻ dự án",
        "project.leave": "Rời dự án",
        "project.share_update": "Cập nhật quyền thành viên",
        "project.unshare": "Thu hồi quyền truy cập",
        "project.export": "Xuất dự án",
        "project.import": "Nhập dự án",
        "project.clone": "Sao chép dự án",
        "device.csv_import": "Nhập thiết bị từ CSV",
        "ai.action": "Thao tác bằng AI",
        "ai.command_blocked": "AI bị chặn lệnh",
        "auth.login": "Đăng nhập",
        "auth.password_change": "Đổi mật khẩu",
        "user.create": "Tạo tài khoản",
        "user.update": "Cập nhật tài khoản",
        "user.password_reset": "Đặt lại mật khẩu",
        "batch.apply": "Áp dụng nhóm thay đổi",
        "batch.approve": "Duyệt nhóm thay đổi",
        "batch.cancel": "Hủy nhóm thay đổi",
        "change.apply": "Áp dụng cấu hình",
        "change.approve": "Duyệt thay đổi",
        "change.cancel": "Hủy thay đổi",
        "change.preview": "Xem trước thay đổi",
        "change.verification_overridden": "Bỏ qua xác minh thay đổi",
        "chat.config_apply_approved": "Áp dụng cấu hình từ chat",
        "chat.config_export": "Xuất cấu hình từ chat",
        "chat.design_applied": "Áp dụng thiết kế mạng từ chat",
        "command.blocked": "Lệnh bị chặn",
        "command.readonly": "Đọc trạng thái thiết bị",
        "device.create": "Thêm thiết bị",
        "device.update": "Cập nhật thiết bị",
        "device.delete": "Xóa thiết bị",
        "device.provision": "Cấp địa chỉ quản lý cho thiết bị",
        "device.restore": "Khôi phục thiết bị",
        "device.test_connection": "Kiểm tra kết nối thiết bị",
        "change.deploy.started": "Bắt đầu triển khai thay đổi",
        "group.auto_approve": "Tự động duyệt nhóm thay đổi",
        "topology.blueprint": "Tạo sơ đồ từ mẫu",
        "topology.chat_diagram_copy": "Sao chép sơ đồ từ chat",
        "topology.chat_diagram_import": "Nhập sơ đồ từ chat",
        "topology.config_preview": "Xem trước cấu hình mạng",
        "topology.design_add": "Thêm thành phần vào sơ đồ",
        "topology.design_delete": "Xóa thành phần khỏi sơ đồ",
        "topology.discover": "Khám phá kết nối mạng",
        "topology.discover_apply": "Áp dụng kết nối đã khám phá",
        "topology.duplicate": "Nhân bản thiết bị",
        "topology.link_create": "Tạo kết nối",
        "topology.link_update": "Cập nhật kết nối",
        "topology.link_delete": "Xóa kết nối",
        "topology.pnetlab_import": "Nhập lab từ PNETLab",
        "topology.version_create": "Lưu phiên bản sơ đồ",
        "topology.version_restore": "Khôi phục phiên bản sơ đồ",
        // Keep labels for events written by earlier UI builds.
        "system.project.update": "Cập nhật dự án",
        "system.ai.action": "Thao tác bằng AI",
        "system.topology.link_create": "Tạo kết nối",
        "system.topology.link_update": "Cập nhật kết nối",
        "system.topology.link_delete": "Xóa kết nối",
        "system.device.create": "Thêm thiết bị",
        "system.device.update": "Cập nhật thiết bị",
        "system.device.delete": "Xóa thiết bị",
      };
      if (labels[action]) return labels[action];
      if (!action) return "Hoạt động";
      return action
        .replace(/[._]+/g, " ")
        .replace(/\b\w/g, (character) => character.toLocaleUpperCase("vi-VN"));
    },

    activityResultLabel(result) {
      return ({ success: "Thành công", failure: "Thất bại", blocked: "Đã chặn" })[result] || result;
    },

    formatActivityDetails(details) {
      if (details === null || details === undefined) return "";
      if (typeof details === "string") return details;
      try { return JSON.stringify(details, null, 2); } catch { return String(details); }
    },

    async refreshActivity(reset = false) {
      if (this.activityLoading) return;
      this.activityLoading = true;
      const projectId = this.currentProjectId;
      const generation = this._activityGeneration;
      try {
        const data = await this.authFetch("/api/dashboard/activity?limit=50");
        if (generation !== this._activityGeneration || projectId !== this.currentProjectId) return;
        const merged = new Map((reset ? [] : this.activityEvents).map((event) => [event.id, event]));
        for (const event of data.items || []) merged.set(event.id, event);
        this.activityEvents = [...merged.values()].sort((a, b) => this.compareActivityEvents(a, b));
        if (reset) this._activityExhausted = false;
        if (reset || !this._activityExhausted) this.activityHasMore = Boolean(data.has_more);
        this.activityError = "";
      } catch (err) {
        if (generation === this._activityGeneration) {
          this.activityError = err.message || "Không tải được nhật ký hoạt động.";
        }
        throw err;
      } finally {
        if (generation === this._activityGeneration) this.activityLoading = false;
      }
    },

    async loadMoreActivity() {
      if (this.activityLoading || !this.activityHasMore) return;
      const oldest = this.activityEvents[this.activityEvents.length - 1];
      if (!oldest) {
        this.activityHasMore = false;
        this._activityExhausted = true;
        return;
      }
      this.activityLoading = true;
      const generation = this._activityGeneration;
      try {
        const params = new URLSearchParams({
          limit: "50",
          before: oldest.created_at,
          before_id: String(oldest.id),
        });
        const data = await this.authFetch(`/api/dashboard/activity?${params.toString()}`);
        if (generation !== this._activityGeneration) return;
        const known = new Set(this.activityEvents.map((event) => event.id));
        const older = (data.items || []).filter((event) => !known.has(event.id));
        this.activityEvents = [...this.activityEvents, ...older].sort((a, b) => this.compareActivityEvents(a, b));
        this.activityHasMore = Boolean(data.has_more && older.length);
        this._activityExhausted = !this.activityHasMore;
        this.activityError = "";
      } catch (err) {
        if (generation === this._activityGeneration) {
          this.activityError = err.message || "Không tải được nhật ký hoạt động.";
        }
        throw err;
      } finally {
        if (generation === this._activityGeneration) this.activityLoading = false;
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
