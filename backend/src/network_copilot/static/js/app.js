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
    _sessionGeneration: 0,

    // -- devices --
    devices: [],
    _deviceRefreshGeneration: 0,

    // -- changes (shared live state, keyed by id) --
    changesById: {},
    _changesRefreshGeneration: 0,

    // -- chat --
    messages: [],
    draftMessage: "",
    sending: false,
    _messagesRefreshGeneration: 0,
    _clientMessageSequence: 0,

    get pendingChanges() {
      return Object.values(this.changesById).filter(
        (change) => change.status === "pending_approval"
      );
    },

    init() {
      if (this.token) {
        const generation = this._sessionGeneration;
        this.authFetch("/api/auth/me")
          .then((user) => {
            if (generation !== this._sessionGeneration) return;
            this.currentUser = user;
            localStorage.setItem("nc_user", JSON.stringify(user));
            return this.startApp();
          })
          .catch(() => {
            if (
              generation === this._sessionGeneration &&
              this.token &&
              this.currentUser
            ) {
              return this.startApp();
            }
          });
      }
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
        error.data = data;
        error.status = response.status;
        throw error;
      }
      return data;
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
      this._deviceRefreshGeneration += 1;
      this._changesRefreshGeneration += 1;
      this._messagesRefreshGeneration += 1;
      this.token = null;
      this.currentUser = null;
      localStorage.removeItem("nc_token");
      localStorage.removeItem("nc_user");
      this.stopPolling();
      this.devices = [];
      this.changesById = {};
      this.messages = [];
      this.draftMessage = "";
      this.sending = false;
    },

    async startApp() {
      const generation = this._sessionGeneration;
      const bootstrap = [
        () => this.hydrateMessages(),
        () => this.refreshDevices(),
        () => this.refreshChanges(),
      ];
      for (const load of bootstrap) {
        try {
          await load();
        } catch {
          if (generation !== this._sessionGeneration) return;
        }
        if (generation !== this._sessionGeneration) return;
      }
      this.startPolling();
    },

    startPolling() {
      this.stopPolling();
      this._deviceTimer = setInterval(() => {
        this.refreshDevices().catch(() => {});
      }, 15000);
      this._changesTimer = setInterval(() => {
        this.refreshChanges().catch(() => {});
      }, 15000);
      this._messagesTimer = setInterval(() => {
        this.pollMessages().catch(() => {});
      }, 7000);
    },

    stopPolling() {
      clearInterval(this._deviceTimer);
      clearInterval(this._changesTimer);
      clearInterval(this._messagesTimer);
    },

    async refreshDevices() {
      const generation = this._deviceRefreshGeneration;
      const data = await this.authFetch("/api/devices");
      if (generation === this._deviceRefreshGeneration) {
        this.devices = data.items;
      }
    },

    async refreshChanges() {
      const generation = this._changesRefreshGeneration;
      const data = await this.authFetch("/api/changes?limit=500");
      if (generation === this._changesRefreshGeneration) {
        this.changesById = Object.fromEntries(
          data.items.map((change) => [change.id, change])
        );
      }
    },

    async approveChange(id) {
      try {
        const generation = this._changesRefreshGeneration;
        const change = await this.authFetch(`/api/changes/${id}/approve`, {
          method: "POST",
        });
        if (generation === this._changesRefreshGeneration) {
          this.changesById[id] = change;
        }
      } catch (err) {
        alert(err.message);
      }
    },

    async applyChange(id) {
      try {
        const generation = this._changesRefreshGeneration;
        const change = await this.authFetch(`/api/changes/${id}/apply`, {
          method: "POST",
        });
        if (generation === this._changesRefreshGeneration) {
          this.changesById[id] = change;
        }
      } catch (err) {
        alert(err.message);
      }
    },

    async cancelChange(id) {
      try {
        const generation = this._changesRefreshGeneration;
        const change = await this.authFetch(`/api/changes/${id}/cancel`, {
          method: "POST",
        });
        if (generation === this._changesRefreshGeneration) {
          this.changesById[id] = change;
        }
      } catch (err) {
        alert(err.message);
      }
    },

    _clientMessage(role, content, payload) {
      this._clientMessageSequence += 1;
      return {
        id: `client-${this._clientMessageSequence}`,
        username: this.currentUser && this.currentUser.username,
        role,
        content,
        payload: payload || null,
        created_at: new Date().toISOString(),
        _client: true,
      };
    },

    _ingestMessage(message) {
      if (!message._client) {
        if (this.messages.some((item) => !item._client && item.id === message.id)) {
          return;
        }
        const clientIndex = this.messages.findIndex(
          (item) =>
            item._client &&
            item.role === message.role &&
            item.content === message.content &&
            (!item.username ||
              !message.username ||
              item.username === message.username)
        );
        if (clientIndex >= 0) {
          this.messages.splice(clientIndex, 1, message);
        } else {
          this.messages.push(message);
        }
      } else {
        this.messages.push(message);
      }
      const change = message.payload && message.payload.change;
      if (change && change.id != null && !(change.id in this.changesById)) {
        this.changesById[change.id] = change;
      }
    },

    _isScrolledToBottom() {
      const el = this.$refs.chatLog;
      if (!el) return true;
      return el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    },

    _scrollToBottom() {
      const el = this.$refs.chatLog;
      if (el) el.scrollTop = el.scrollHeight;
    },

    async hydrateMessages() {
      const generation = this._messagesRefreshGeneration;
      const data = await this.authFetch("/api/chat/messages");
      if (generation !== this._messagesRefreshGeneration) return;
      for (const message of data.items) this._ingestMessage(message);
      this.$nextTick(() => {
        if (generation === this._messagesRefreshGeneration) {
          this._scrollToBottom();
        }
      });
    },

    async pollMessages() {
      const generation = this._messagesRefreshGeneration;
      const wasAtBottom = this._isScrolledToBottom();
      const data = await this.authFetch("/api/chat/messages");
      if (generation !== this._messagesRefreshGeneration) return;
      const known = new Set(this.messages.map((message) => message.id));
      let appended = false;
      for (const message of data.items) {
        if (!known.has(message.id)) {
          this._ingestMessage(message);
          known.add(message.id);
          appended = true;
        }
      }
      if (appended && wasAtBottom) {
        this.$nextTick(() => {
          if (generation === this._messagesRefreshGeneration) {
            this._scrollToBottom();
          }
        });
      }
    },

    async sendMessage() {
      const text = this.draftMessage.trim();
      if (!text || this.sending) return;
      const generation = this._messagesRefreshGeneration;
      this.draftMessage = "";
      this.sending = true;
      this._ingestMessage(this._clientMessage("user", text, null));
      this.$nextTick(() => {
        if (generation === this._messagesRefreshGeneration) {
          this._scrollToBottom();
        }
      });
      try {
        const payload = await this.authFetch("/api/ai/chat", {
          method: "POST",
          body: JSON.stringify({ message: text }),
        });
        if (generation !== this._messagesRefreshGeneration) return;
        this._ingestMessage(
          this._clientMessage(
            "assistant",
            payload.explanation || "Request completed.",
            payload
          )
        );
      } catch (err) {
        if (generation !== this._messagesRefreshGeneration) return;
        const payload =
          err.data && typeof err.data === "object" && !Array.isArray(err.data)
            ? err.data
            : { error: "request_failed", message: err.message };
        this._ingestMessage(
          this._clientMessage(
            "system",
            typeof payload.message === "string"
              ? payload.message
              : "Request failed.",
            payload
          )
        );
      } finally {
        if (generation !== this._messagesRefreshGeneration) return;
        try {
          await this.pollMessages();
        } catch {
          // The POST result is already present as a client message. A later
          // scheduled poll can reconcile it with persisted transcript rows.
        }
        if (generation === this._messagesRefreshGeneration) {
          this.sending = false;
        }
      }
    },
  }));
});
