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
    _authRetryTimer: null,

    // -- devices --
    devices: [],
    _deviceRefreshGeneration: 0,

    // -- changes (shared live state, keyed by id) --
    changesById: {},
    _changesRefreshGeneration: 0,
    changesLoading: false,
    changesError: "",
    // Draft text for the "type the hostname to confirm" box a dangerous
    // change shows before Apply. Keyed by change id, kept separate from
    // changesById so a poll refreshing that map never wipes what the
    // operator is mid-typing.
    confirmInputs: {},

    // -- change batches (parents own their child changes) --
    batchesById: {},
    batchConfirmInputs: {},
    batchActionIds: {},
    batchActionErrors: {},
    _batchesRefreshGeneration: 0,
    _batchesRefreshRequestToken: 0,
    batchesLoading: false,
    batchesError: "",

    // -- chat --
    messages: [],
    draftMessage: "",
    sending: false,
    _messagesRefreshGeneration: 0,
    _clientMessageSequence: 0,
    // ISO timestamp string, or null. Set by startNewChat(), cleared by
    // showFullHistory() and by logout(). Persisted to localStorage so it
    // survives a page refresh, but never sent to the server: this hides
    // history in this browser only, per the design's explicit scope.
    chatCutoff: localStorage.getItem("nc_chat_cutoff") || null,

    get pendingChanges() {
      return Object.values(this.changesById).filter(
        (change) => change.status === "pending_approval"
      );
    },

    get pendingBatches() {
      return Object.values(this.batchesById).filter(
        (batch) =>
          batch.status === "pending_approval" || batch.status === "approved"
      );
    },

    get visibleMessages() {
      if (!this.chatCutoff) return this.messages;
      // Reuse _messageTimestamp() (defined further down in this component)
      // rather than comparing created_at strings directly: it normalises
      // server timestamps that arrive without a timezone suffix, which a
      // naive string comparison against an always-suffixed chatCutoff
      // (from toISOString()) would get wrong.
      const cutoffTime = Date.parse(this.chatCutoff);
      return this.messages.filter((message) => {
        const timestamp = this._messageTimestamp(message);
        return timestamp === null || timestamp > cutoffTime;
      });
    },

    get hiddenMessageCount() {
      return this.messages.length - this.visibleMessages.length;
    },

    startNewChat() {
      this.chatCutoff = new Date().toISOString();
      localStorage.setItem("nc_chat_cutoff", this.chatCutoff);
    },

    showFullHistory() {
      this.chatCutoff = null;
      localStorage.removeItem("nc_chat_cutoff");
    },

    init() {
      if (!this.token) return Promise.resolve();
      const generation = this._sessionGeneration;
      const token = this.token;
      return this.authFetch("/api/auth/me")
        .then((user) => {
          if (
            generation !== this._sessionGeneration ||
            token !== this.token
          ) return;
          clearTimeout(this._authRetryTimer);
          this._authRetryTimer = null;
          this.currentUser = user;
          localStorage.setItem("nc_user", JSON.stringify(user));
          return this.startApp();
        })
        .catch(() => {
          if (
            generation !== this._sessionGeneration ||
            token !== this.token
          ) return;
          if (this.currentUser) return this.startApp();
          clearTimeout(this._authRetryTimer);
          this._authRetryTimer = setTimeout(() => {
            if (
              generation === this._sessionGeneration &&
              token === this.token &&
              !this.currentUser
            ) {
              return this.init();
            }
          }, 5000);
        });
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
        clearTimeout(this._authRetryTimer);
        this._authRetryTimer = null;
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
      this._batchesRefreshGeneration += 1;
      this._batchesRefreshRequestToken += 1;
      this._messagesRefreshGeneration += 1;
      this.token = null;
      this.currentUser = null;
      clearTimeout(this._authRetryTimer);
      this._authRetryTimer = null;
      localStorage.removeItem("nc_token");
      localStorage.removeItem("nc_user");
      this.stopPolling();
      this.devices = [];
      this.changesById = {};
      this.confirmInputs = {};
      this.changesLoading = false;
      this.changesError = "";
      this.batchesById = {};
      this.batchConfirmInputs = {};
      this.batchActionIds = {};
      this.batchActionErrors = {};
      this.batchesLoading = false;
      this.batchesError = "";
      this.messages = [];
      this.draftMessage = "";
      this.sending = false;
      this.chatCutoff = null;
      localStorage.removeItem("nc_chat_cutoff");
    },

    async startApp() {
      const generation = this._sessionGeneration;
      this.changesLoading = true;
      this.batchesLoading = true;
      const bootstrap = [
        () => this.hydrateMessages(),
        () => this.refreshDevices(),
        () => this.refreshChanges(),
        () => this.refreshBatches(),
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
      this._batchesTimer = setInterval(() => {
        this.refreshBatches().catch(() => {});
      }, 15000);
      this._messagesTimer = setInterval(() => {
        this.pollMessages().catch(() => {});
      }, 7000);
    },

    stopPolling() {
      clearInterval(this._deviceTimer);
      clearInterval(this._changesTimer);
      clearInterval(this._batchesTimer);
      clearInterval(this._messagesTimer);
      this._deviceTimer = null;
      this._changesTimer = null;
      this._batchesTimer = null;
      this._messagesTimer = null;
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
      this.changesLoading = true;
      this.changesError = "";
      try {
        const data = await this.authFetch(
          "/api/changes?standalone_only=true&limit=500"
        );
        if (generation === this._changesRefreshGeneration) {
          this.changesById = Object.fromEntries(
            data.items.map((change) => [change.id, change])
          );
        }
      } catch (err) {
        if (generation === this._changesRefreshGeneration) {
          this.changesError = err.message || "Could not load changes.";
        }
        throw err;
      } finally {
        if (generation === this._changesRefreshGeneration) {
          this.changesLoading = false;
        }
      }
    },

    async refreshBatches() {
      if (Object.keys(this.batchActionIds).length > 0) return;
      const generation = this._batchesRefreshGeneration;
      const requestToken = ++this._batchesRefreshRequestToken;
      this.batchesLoading = true;
      this.batchesError = "";
      try {
        const data = await this.authFetch("/api/change-batches?limit=500");
        if (
          generation === this._batchesRefreshGeneration &&
          requestToken === this._batchesRefreshRequestToken
        ) {
          this.batchesById = Object.fromEntries(
            data.items.map((batch) => [batch.id, batch])
          );
        }
      } catch (err) {
        if (
          generation === this._batchesRefreshGeneration &&
          requestToken === this._batchesRefreshRequestToken
        ) {
          this.batchesError = err.message || "Could not load batches.";
        }
        throw err;
      } finally {
        if (
          generation === this._batchesRefreshGeneration &&
          requestToken === this._batchesRefreshRequestToken
        ) {
          this.batchesLoading = false;
        }
      }
    },

    async approveBatch(id) {
      if (this.batchActionIds[id]) return;
      const generation = this._sessionGeneration;
      this._batchesRefreshGeneration += 1;
      this._batchesRefreshRequestToken += 1;
      this.batchesLoading = false;
      this.batchActionIds[id] = "approve";
      delete this.batchActionErrors[id];
      try {
        const batch = await this.authFetch(
          `/api/change-batches/${id}/approve`,
          { method: "POST" }
        );
        if (
          generation === this._sessionGeneration &&
          this.batchActionIds[id] === "approve"
        ) {
          this.batchesById[id] = batch;
        }
      } catch (err) {
        if (
          generation === this._sessionGeneration &&
          this.batchActionIds[id] === "approve"
        ) {
          this.batchActionErrors[id] = err.message || "Approval failed.";
        }
      } finally {
        if (
          generation === this._sessionGeneration &&
          this.batchActionIds[id] === "approve"
        ) {
          delete this.batchActionIds[id];
        }
      }
    },

    batchConfirmationMatches(id) {
      const batch = this.batchesById[id];
      if (!batch) return false;
      if (!batch.requires_confirmation) return true;
      if (typeof batch.confirmation_text !== "string") return false;
      return (this.batchConfirmInputs[id] || "") === batch.confirmation_text;
    },

    batchOutcomeCount(batch, status) {
      return (batch && Array.isArray(batch.changes) ? batch.changes : []).filter(
        (change) => change.status === status
      ).length;
    },

    batchVerificationPlan(child) {
      if (!child || !Array.isArray(child.verification_commands)) return [];
      return child.verification_commands.filter(
        (command) => typeof command === "string" && command.trim() !== ""
      );
    },

    batchVerificationResults(child) {
      const verification = child && child.verification_output;
      if (
        !verification ||
        typeof verification !== "object" ||
        Array.isArray(verification)
      ) {
        return [];
      }
      return Object.entries(verification).map(([command, result]) => {
        const value = result && typeof result === "object" ? result : {};
        const redacted = Boolean(
          value.redacted || value.is_redacted || value.output_redacted
        );
        const details = Array.isArray(value.details)
          ? value.details.filter((detail) => typeof detail === "string")
          : typeof value.details === "string"
            ? [value.details]
            : [];
        return {
          command,
          status: value.passed ? "passed" : "failed",
          details,
          output: redacted
            ? "Verification output redacted for safety."
            : typeof value.output === "string"
              ? value.output
              : "",
          redacted,
        };
      });
    },

    async applyBatch(id) {
      if (this.batchActionIds[id]) return;
      const batch = this.batchesById[id];
      if (!batch || !this.batchConfirmationMatches(id)) return;
      const generation = this._sessionGeneration;
      this._batchesRefreshGeneration += 1;
      this._batchesRefreshRequestToken += 1;
      this.batchesLoading = false;
      this.batchActionIds[id] = "apply";
      delete this.batchActionErrors[id];
      try {
        const body = {};
        if (batch.requires_confirmation) {
          body.confirmation = this.batchConfirmInputs[id] || "";
        }
        const updated = await this.authFetch(
          `/api/change-batches/${id}/apply`,
          {
            method: "POST",
            body: JSON.stringify(body),
          }
        );
        if (
          generation === this._sessionGeneration &&
          this.batchActionIds[id] === "apply"
        ) {
          this.batchesById[id] = updated;
          delete this.batchConfirmInputs[id];
        }
      } catch (err) {
        if (
          generation === this._sessionGeneration &&
          this.batchActionIds[id] === "apply"
        ) {
          this.batchActionErrors[id] = err.message || "Apply failed.";
        }
      } finally {
        if (
          generation === this._sessionGeneration &&
          this.batchActionIds[id] === "apply"
        ) {
          delete this.batchActionIds[id];
        }
      }
    },

    async cancelBatch(id) {
      if (this.batchActionIds[id]) return;
      const generation = this._sessionGeneration;
      this._batchesRefreshGeneration += 1;
      this._batchesRefreshRequestToken += 1;
      this.batchesLoading = false;
      this.batchActionIds[id] = "cancel";
      delete this.batchActionErrors[id];
      try {
        const batch = await this.authFetch(
          `/api/change-batches/${id}/cancel`,
          { method: "POST" }
        );
        if (
          generation === this._sessionGeneration &&
          this.batchActionIds[id] === "cancel"
        ) {
          this.batchesById[id] = batch;
          delete this.batchConfirmInputs[id];
        }
      } catch (err) {
        if (
          generation === this._sessionGeneration &&
          this.batchActionIds[id] === "cancel"
        ) {
          this.batchActionErrors[id] = err.message || "Cancellation failed.";
        }
      } finally {
        if (
          generation === this._sessionGeneration &&
          this.batchActionIds[id] === "cancel"
        ) {
          delete this.batchActionIds[id];
        }
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

    confirmHostnameMatches(id) {
      const change = this.changesById[id];
      const expected = change && change.device && change.device.hostname;
      if (!expected) return false;
      return (this.confirmInputs[id] || "").trim() === expected;
    },

    resultColumns(result) {
      if (!Array.isArray(result.parsed) || result.parsed.length === 0) return [];
      return Object.keys(result.parsed[0]);
    },

    resultCellLabel(column) {
      return column
        .split("_")
        .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
        .join(" ");
    },

    resultCellValue(row, column) {
      const value = row[column];
      if (Array.isArray(value)) return value.join(", ");
      if (value === null || value === undefined || value === "") return "—";
      return value;
    },

    async applyChange(id) {
      try {
        const generation = this._changesRefreshGeneration;
        const change = this.changesById[id];
        const body = {};
        if (change && change.requires_confirmation) {
          if (!this.confirmHostnameMatches(id)) {
            // The Apply button is disabled until this matches, but guard the
            // request too in case state changed between render and click.
            return;
          }
          body.confirm_hostname = (this.confirmInputs[id] || "").trim();
        }
        const updated = await this.authFetch(`/api/changes/${id}/apply`, {
          method: "POST",
          body: JSON.stringify(body),
        });
        if (generation === this._changesRefreshGeneration) {
          this.changesById[id] = updated;
          delete this.confirmInputs[id];
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

    _clientMessage(role, content, payload, knownServerIds = []) {
      this._clientMessageSequence += 1;
      return {
        id: `client-${this._clientMessageSequence}`,
        username: this.currentUser && this.currentUser.username,
        role,
        content,
        payload: payload || null,
        created_at: new Date().toISOString(),
        _client: true,
        _knownServerIds: knownServerIds,
      };
    },

    _messagesMatch(clientMessage, serverMessage) {
      return (
        clientMessage.role === serverMessage.role &&
        clientMessage.content === serverMessage.content &&
        (!clientMessage.username ||
          !serverMessage.username ||
          clientMessage.username === serverMessage.username) &&
        !(clientMessage._knownServerIds || []).includes(serverMessage.id)
      );
    },

    _messageTimestamp(message) {
      if (!message.created_at) return null;
      let value = message.created_at;
      if (!message._client && !/(?:Z|[+-]\d\d:\d\d)$/i.test(value)) {
        value += "Z";
      }
      const timestamp = Date.parse(value);
      return Number.isFinite(timestamp) ? timestamp : null;
    },

    _sortMessages() {
      this.messages.sort((left, right) => {
        const leftTime = this._messageTimestamp(left);
        const rightTime = this._messageTimestamp(right);
        if (leftTime !== null && rightTime !== null && leftTime !== rightTime) {
          return leftTime - rightTime;
        }
        if (
          !left._client &&
          !right._client &&
          typeof left.id === "number" &&
          typeof right.id === "number"
        ) {
          return left.id - right.id;
        }
        return 0;
      });
    },

    _ingestMessage(message) {
      if (!message._client) {
        if (this.messages.some((item) => !item._client && item.id === message.id)) {
          return;
        }
        const clientIndex = this.messages.findIndex(
          (item) => item._client && this._messagesMatch(item, message)
        );
        if (clientIndex >= 0) {
          this.messages.splice(clientIndex, 1, message);
        } else {
          this.messages.push(message);
        }
      } else {
        const serverMatch = this.messages.some(
          (item) => !item._client && this._messagesMatch(message, item)
        );
        if (serverMatch) return;
        this.messages.push(message);
      }
      this._sortMessages();
      const change = message.payload && message.payload.change;
      if (change && change.id != null && !(change.id in this.changesById)) {
        this.changesById[change.id] = change;
      }
      const batch = message.payload && message.payload.batch;
      if (batch && batch.id != null && !(batch.id in this.batchesById)) {
        this.batchesById[batch.id] = batch;
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
      const knownServerIds = this.messages
        .filter((message) => !message._client)
        .map((message) => message.id);
      this.draftMessage = "";
      this.sending = true;
      this._ingestMessage(
        this._clientMessage("user", text, null, knownServerIds)
      );
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
            payload,
            knownServerIds
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
            payload,
            knownServerIds
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
