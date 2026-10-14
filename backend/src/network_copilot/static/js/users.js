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
  Alpine.data("usersApp", () => ({
    token: localStorage.getItem("nc_token") || null,
    currentUser: storedUser(),
    loginForm: { username: "", password: "" },
    loginError: "",
    _sessionGeneration: 0,

    users: [],
    createForm: { username: "", full_name: "", email: "", password: "", role: "OPERATOR" },
    editing: null, // copy of the user being edited
    error: "",
    notice: "",
    passwordForm: { current_password: "", new_password: "", repeat: "" },
    passwordError: "",
    passwordNotice: "",

    init() {
      if (!this.token) return;
      this.startApp();
    },

    get isAdmin() {
      return !!this.currentUser && this.currentUser.role === "ADMIN";
    },

    async authFetch(path, options = {}) {
      const token = this.token;
      const generation = this._sessionGeneration;
      const headers = Object.assign(
        { "Content-Type": "application/json" },
        options.headers || {},
        token ? { Authorization: `Bearer ${token}` } : {}
      );
      const response = await fetch(path, { ...options, headers });
      const data =
        response.status === 204 ? {} : await response.json().catch(() => ({}));
      if (
        response.status === 401 &&
        generation === this._sessionGeneration &&
        token === this.token &&
        path !== "/api/auth/login"
      ) {
        this.logout();
      }
      if (!response.ok) {
        const error = new Error((data && data.message) || `Request failed (${response.status})`);
        error.status = response.status;
        error.data = data;
        throw error;
      }
      return data;
    },

    describeError(err) {
      const details = err && err.data && err.data.details;
      if (details && typeof details === "object") {
        const parts = Object.entries(details)
          .filter(([, messages]) => Array.isArray(messages))
          .map(([field, messages]) => `${field}: ${messages.join(", ")}`);
        if (parts.length) return `${err.message} (${parts.join("; ")})`;
      }
      return (err && err.message) || "Có lỗi xảy ra.";
    },

    async login() {
      this.loginError = "";
      try {
        const data = await this.authFetch("/api/auth/login", {
          method: "POST",
          body: JSON.stringify(this.loginForm),
        });
        this.setSession(data.access_token, data.user);
        this.loginForm = { username: "", password: "" };
        await this.startApp();
      } catch (err) {
        this.loginError = err.message || "Đăng nhập thất bại.";
      }
    },

    setSession(token, user) {
      this.token = token;
      this.currentUser = user;
      localStorage.setItem("nc_token", token);
      localStorage.setItem("nc_user", JSON.stringify(user));
    },

    logout() {
      this._sessionGeneration += 1;
      this.token = null;
      this.currentUser = null;
      this.users = [];
      localStorage.removeItem("nc_token");
      localStorage.removeItem("nc_user");
      localStorage.removeItem("nc_project_id");
    },

    async startApp() {
      try {
        this.currentUser = await this.authFetch("/api/auth/me");
        localStorage.setItem("nc_user", JSON.stringify(this.currentUser));
        if (this.isAdmin) await this.loadUsers();
      } catch (err) {
        this.error = this.describeError(err);
      }
    },

    async loadUsers() {
      const data = await this.authFetch("/api/users");
      this.users = data.items;
    },

    async createUser() {
      this.error = "";
      this.notice = "";
      try {
        const body = { ...this.createForm };
        for (const key of ["full_name", "email"]) if (!body[key]) delete body[key];
        const created = await this.authFetch("/api/users", {
          method: "POST",
          body: JSON.stringify(body),
        });
        this.createForm = { username: "", full_name: "", email: "", password: "", role: "OPERATOR" };
        this.notice = `Đã tạo tài khoản ${created.username}.`;
        await this.loadUsers();
      } catch (err) {
        this.error = this.describeError(err);
      }
    },

    startEdit(user) {
      this.error = "";
      this.notice = "";
      this.editing = {
        id: user.id,
        username: user.username,
        full_name: user.full_name || "",
        email: user.email || "",
        role: user.role,
        is_active: user.is_active,
      };
    },

    async saveEdit() {
      this.error = "";
      try {
        const { id, full_name, email, role, is_active } = this.editing;
        await this.authFetch(`/api/users/${id}`, {
          method: "PUT",
          body: JSON.stringify({
            full_name: full_name || null,
            email: email || null,
            role,
            is_active,
          }),
        });
        this.notice = `Đã cập nhật ${this.editing.username}.`;
        this.editing = null;
        await this.loadUsers();
      } catch (err) {
        this.error = this.describeError(err);
      }
    },

    async toggleActive(user) {
      this.error = "";
      this.notice = "";
      try {
        await this.authFetch(`/api/users/${user.id}`, {
          method: "PUT",
          body: JSON.stringify({ is_active: !user.is_active }),
        });
        this.notice = `${user.username} đã ${user.is_active ? "bị vô hiệu hoá" : "được kích hoạt lại"}.`;
        await this.loadUsers();
      } catch (err) {
        this.error = this.describeError(err);
      }
    },

    async resetPassword(user) {
      const value = window.prompt(`Mật khẩu mới cho ${user.username} (≥ 10 ký tự):`);
      if (!value) return;
      this.error = "";
      this.notice = "";
      try {
        await this.authFetch(`/api/users/${user.id}/reset-password`, {
          method: "POST",
          body: JSON.stringify({ new_password: value }),
        });
        this.notice = `Đã đặt lại mật khẩu của ${user.username}; họ bị đăng xuất khỏi mọi phiên.`;
      } catch (err) {
        this.error = this.describeError(err);
      }
    },

    async changePassword() {
      this.passwordError = "";
      this.passwordNotice = "";
      const form = this.passwordForm;
      if (form.new_password !== form.repeat) {
        this.passwordError = "Mật khẩu nhập lại không khớp.";
        return;
      }
      try {
        const data = await this.authFetch("/api/auth/change-password", {
          method: "POST",
          body: JSON.stringify({
            current_password: form.current_password,
            new_password: form.new_password,
          }),
        });
        // Every older token was just revoked; keep working with the new one.
        this.setSession(data.access_token, data.user);
        this.passwordForm = { current_password: "", new_password: "", repeat: "" };
        this.passwordNotice = "Đã đổi mật khẩu.";
      } catch (err) {
        this.passwordError = this.describeError(err);
      }
    },
  }));
});
