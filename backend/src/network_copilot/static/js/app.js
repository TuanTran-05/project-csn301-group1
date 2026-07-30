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
  Alpine.data("app", () => ({
    // -- auth state --
    token: localStorage.getItem("nc_token") || null,
    currentUser: storedUser(),
    loginForm: { username: "", password: "" },
    loginError: "",

    init() {
      if (this.token) {
        this.authFetch("/api/auth/me")
          .then((user) => {
            this.currentUser = user;
            localStorage.setItem("nc_user", JSON.stringify(user));
            return this.startApp();
          })
          .catch(() => {
            /* authFetch already logged out on a 401 */
          });
      }
    },

    async authFetch(path, options = {}) {
      const headers = Object.assign(
        { "Content-Type": "application/json" },
        options.headers || {},
        this.token ? { Authorization: `Bearer ${this.token}` } : {}
      );
      const response = await fetch(path, { ...options, headers });
      const data = await response.json().catch(() => ({}));
      if (response.status === 401) {
        this.logout();
        throw new Error(data.message || "session_expired");
      }
      if (!response.ok) {
        throw new Error(data.message || `Request failed (${response.status})`);
      }
      return data;
    },

    async login() {
      this.loginError = "";
      try {
        const data = await this.authFetch("/api/auth/login", {
          method: "POST",
          body: JSON.stringify(this.loginForm),
        });
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
      this.token = null;
      this.currentUser = null;
      localStorage.removeItem("nc_token");
      localStorage.removeItem("nc_user");
    },

    // Filled in by Task 8 (devices), Task 9 (changes), Task 10 (chat).
    async startApp() {},
  }));
});
