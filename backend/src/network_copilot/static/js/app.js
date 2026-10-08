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

    // -- projects (the isolation boundary: devices, chat and changes all
    // belong to the selected project) --
    projects: [],
    currentProjectId: (() => {
      const stored = localStorage.getItem("nc_project_id");
      return stored ? Number(stored) : null;
    })(),
    noProject: false,

    // -- live updates (server-sent events; polling is the fallback) --
    _streamGeneration: 0,
    _streamAbort: null,
    _streamHealthy: false,
    _streamState: null,

    // -- chat composer --
    showJump: false,
    _sentHistory: [],
    _historyIndex: -1,

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
    sessions: [],
    // A per-browser bookmark of which shared session this browser was last
    // looking at (see the design spec: sessions are shared team data, but
    // which one *you* are currently viewing is a per-browser preference,
    // same role nc_token/nc_user already play for auth state).
    currentSessionId: (() => {
      const stored = localStorage.getItem("nc_session_id");
      return stored ? Number(stored) : null;
    })(),

    // Starter prompts built from this project's real devices, so a new chat
    // is never a blank box.
    get suggestions() {
      const byRole = (...roles) => this.devices.find((d) => roles.includes(d.role));
      const router = byRole("core", "isp");
      const switchDevice = byRole("access", "distribution");
      const prompts = [];
      if (router) {
        prompts.push(`Kiểm tra trạng thái OSPF trên ${router.hostname}`);
        prompts.push(`Hiển thị bảng định tuyến của ${router.hostname}`);
      }
      if (switchDevice) {
        prompts.push(`Liệt kê VLAN trên ${switchDevice.hostname}`);
        prompts.push(`Xem trạng thái các cổng của ${switchDevice.hostname}`);
      }
      if (this.devices.length) {
        prompts.push("Thiết bị nào đang offline?");
        if (this.currentUser && this.currentUser.role === "ADMIN") {
          prompts.push("Lưu cấu hình trên toàn bộ thiết bị");
        }
      } else {
        prompts.push("Mạng của tôi cần những gì để bắt đầu?");
      }
      return prompts;
    },

    useSuggestion(text) {
      this.draftMessage = text;
      return this.sendMessage();
    },

    insertHostname(hostname) {
      const sep = this.draftMessage && !/\s$/.test(this.draftMessage) ? " " : "";
      this.draftMessage = `${this.draftMessage}${sep}${hostname} `;
      this.$nextTick(() => {
        const box = this.$refs.composer;
        if (box) {
          box.focus();
          this.autoGrow(box);
        }
      });
    },

    // Enter sends, Shift+Enter breaks the line. Enter while an input method
    // (Vietnamese Telex/VNI) is composing only confirms the word, so it must
    // not send.
    onComposerKeydown(event) {
      if (event.isComposing || event.keyCode === 229) return;
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        this.sendMessage();
        return;
      }
      const box = event.target;
      const atStart = box.selectionStart === 0 && box.selectionEnd === 0;
      if (event.key === "ArrowUp" && atStart && this._sentHistory.length) {
        event.preventDefault();
        this._historyIndex = Math.min(
          this._historyIndex + 1,
          this._sentHistory.length - 1
        );
        this.draftMessage = this._sentHistory[this._historyIndex];
        this.$nextTick(() => this.autoGrow(box));
      } else if (event.key === "ArrowDown" && this._historyIndex >= 0) {
        const atEnd = box.selectionStart === box.value.length;
        if (!atEnd) return;
        event.preventDefault();
        this._historyIndex -= 1;
        this.draftMessage =
          this._historyIndex >= 0 ? this._sentHistory[this._historyIndex] : "";
        this.$nextTick(() => this.autoGrow(box));
      }
    },

    autoGrow(box) {
      if (!box) return;
      box.style.height = "auto";
      box.style.height = `${Math.min(box.scrollHeight, 160)}px`;
    },

    onChatScroll() {
      this.showJump = !this._isScrolledToBottom();
    },

    async copyText(text, event) {
      try {
        await navigator.clipboard.writeText(text);
        const button = event && event.target;
        if (button) {
          const label = button.textContent;
          button.textContent = "Đã chép";
          setTimeout(() => {
            button.textContent = label;
          }, 1200);
        }
      } catch {
        // Clipboard access can be blocked (plain http); the text stays selectable.
      }
    },

    get currentProject() {
      return this.projects.find((p) => p.id === this.currentProjectId) || null;
    },

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
      const projectId = this.currentProjectId;
      const headers = Object.assign(
        { "Content-Type": "application/json" },
        projectId ? { "X-Project-Id": String(projectId) } : {},
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

    _resetProjectData() {
      this.stopPolling();
      this._deviceRefreshGeneration += 1;
      this._changesRefreshGeneration += 1;
      this._batchesRefreshGeneration += 1;
      this._batchesRefreshRequestToken += 1;
      this._messagesRefreshGeneration += 1;
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
      this.sessions = [];
      this.currentSessionId = null;
      localStorage.removeItem("nc_session_id");
    },

    async loadProjects() {
      const data = await this.authFetch("/api/projects");
      this.projects = data.items;
      if (this.projects.length === 0) {
        this.currentProjectId = null;
        localStorage.removeItem("nc_project_id");
        this.noProject = true;
        return;
      }
      this.noProject = false;
      if (!this.projects.some((p) => p.id === this.currentProjectId)) {
        this.currentProjectId = this.projects[0].id;
      }
      localStorage.setItem("nc_project_id", String(this.currentProjectId));
    },

    async switchProject(projectId) {
      if (projectId === this.currentProjectId) return;
      this._sessionGeneration += 1;
      this._resetProjectData();
      this.currentProjectId = projectId;
      localStorage.setItem("nc_project_id", String(projectId));
      await this.startApp({ skipProjects: true });
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
      this.sessions = [];
      this.currentSessionId = null;
      localStorage.removeItem("nc_session_id");
      this.projects = [];
      this.currentProjectId = null;
      this.noProject = false;
      localStorage.removeItem("nc_project_id");
    },

    async startApp(options = {}) {
      const generation = this._sessionGeneration;
      if (!options.skipProjects) {
        try {
          await this.loadProjects();
        } catch {
          if (generation !== this._sessionGeneration) return;
        }
        if (generation !== this._sessionGeneration) return;
      }
      if (this.noProject) return;
      this.changesLoading = true;
      this.batchesLoading = true;
      const bootstrap = [
        () => this.loadSessions(),
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
      this.startRealtime();
    },

    startPolling() {
      this.stopPolling();
      // While the event stream is healthy the server says when to refresh, so
      // these timers only act when it is not.
      const unlessStreaming = (refresh) => () => {
        if (!this._streamHealthy) refresh().catch(() => {});
      };
      this._deviceTimer = setInterval(unlessStreaming(() => this.refreshDevices()), 15000);
      this._changesTimer = setInterval(unlessStreaming(() => this.refreshChanges()), 15000);
      this._batchesTimer = setInterval(unlessStreaming(() => this.refreshBatches()), 15000);
      this._messagesTimer = setInterval(unlessStreaming(() => this.pollMessages()), 7000);
      // A slow full refresh as a safety net against a missed event.
      this._safetyTimer = setInterval(() => this._refreshEverything(), 60000);
    },

    _refreshEverything() {
      return Promise.allSettled([
        this.refreshDevices(),
        this.refreshChanges(),
        this.refreshBatches(),
        this.pollMessages(),
      ]);
    },

    // ---- server-sent events over fetch (EventSource cannot send our headers) ----

    startRealtime() {
      this.stopRealtime();
      if (
        !this.currentProjectId ||
        typeof AbortController === "undefined" ||
        typeof TextDecoder === "undefined"
      ) {
        return;
      }
      const generation = ++this._streamGeneration;
      this._streamState = null;
      this._streamLoop(generation);
    },

    stopRealtime() {
      this._streamGeneration += 1;
      this._streamHealthy = false;
      if (this._streamAbort) this._streamAbort.abort();
      this._streamAbort = null;
    },

    async _streamLoop(generation) {
      let failures = 0;
      while (generation === this._streamGeneration && this.token) {
        const controller = new AbortController();
        this._streamAbort = controller;
        try {
          const response = await fetch(`/api/projects/${this.currentProjectId}/events`, {
            headers: {
              Authorization: `Bearer ${this.token}`,
              "X-Project-Id": String(this.currentProjectId),
            },
            signal: controller.signal,
          });
          if (!response.ok || !response.body) throw new Error(String(response.status));
          failures = 0;
          this._streamHealthy = true;
          await this._readStream(response.body, generation);
        } catch {
          if (generation !== this._streamGeneration) return;
          failures += 1;
        }
        this._streamHealthy = false;
        if (generation !== this._streamGeneration) return;
        // A normal end (the server closes after ~55s) reconnects at once;
        // errors back off, and polling covers the gap.
        const wait = failures === 0 ? 300 : Math.min(30000, 1000 * 2 ** Math.min(failures, 5));
        await new Promise((resolve) => setTimeout(resolve, wait));
      }
    },

    async _readStream(body, generation) {
      const reader = body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (generation === this._streamGeneration) {
        const { value, done } = await reader.read();
        if (done) return;
        buffer += decoder.decode(value, { stream: true });
        let end;
        while ((end = buffer.indexOf("\n\n")) >= 0) {
          const block = buffer.slice(0, end);
          buffer = buffer.slice(end + 2);
          this._handleStreamBlock(block);
        }
      }
    },

    _handleStreamBlock(block) {
      let name = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) name = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (name !== "state" || !data) return;
      try {
        this._onRemoteState(JSON.parse(data));
      } catch {
        // A garbled event is ignored; the safety refresh catches up.
      }
    },

    // Refetch only the parts whose fingerprint moved. The first state after
    // connecting refreshes everything once, closing the gap since startApp.
    _onRemoteState(next) {
      const previous = this._streamState;
      this._streamState = next;
      const changed = (key) => !previous || previous[key] !== next[key];
      if (changed("devices")) this.refreshDevices().catch(() => {});
      if (changed("changes")) this.refreshChanges().catch(() => {});
      if (changed("batches")) this.refreshBatches().catch(() => {});
      if (changed("messages")) this.pollMessages().catch(() => {});
    },

    stopPolling() {
      clearInterval(this._deviceTimer);
      clearInterval(this._changesTimer);
      clearInterval(this._batchesTimer);
      clearInterval(this._messagesTimer);
      if (this._safetyTimer) clearInterval(this._safetyTimer);
      this._safetyTimer = null;
      this.stopRealtime();
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

    async loadSessions() {
      const data = await this.authFetch("/api/chat/sessions");
      this.sessions = data.items;
      if (this.sessions.length === 0) {
        const session = await this.authFetch("/api/chat/sessions", {
          method: "POST",
        });
        this.sessions = [session];
      }
      const stillExists =
        this.currentSessionId != null &&
        this.sessions.some((session) => session.id === this.currentSessionId);
      if (!stillExists) {
        this.currentSessionId = this.sessions[0].id;
      }
      localStorage.setItem("nc_session_id", String(this.currentSessionId));
    },

    async switchSession(sessionId) {
      if (sessionId === this.currentSessionId) return;
      this._messagesRefreshGeneration += 1;
      this.currentSessionId = sessionId;
      localStorage.setItem("nc_session_id", String(sessionId));
      this.messages = [];
      try {
        await this.hydrateMessages();
      } catch {
        // A later scheduled poll can retry.
      }
    },

    async startNewChat() {
      try {
        const session = await this.authFetch("/api/chat/sessions", {
          method: "POST",
        });
        this.sessions.unshift(session);
        await this.switchSession(session.id);
      } catch (err) {
        alert(err.message);
      }
    },

    async hydrateMessages() {
      const generation = this._messagesRefreshGeneration;
      const data = await this.authFetch(
        `/api/chat/messages?session_id=${this.currentSessionId}`
      );
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
      const data = await this.authFetch(
        `/api/chat/messages?session_id=${this.currentSessionId}`
      );
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
      this._sentHistory.unshift(text);
      this._sentHistory = this._sentHistory.slice(0, 30);
      this._historyIndex = -1;
      this.sending = true;
      this.$nextTick(() => this.autoGrow(this.$refs.composer));
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
          body: JSON.stringify({
            message: text,
            session_id: this.currentSessionId,
          }),
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
          // Give the box back to the user so the next question needs no click.
          this.$nextTick(() => this.$refs.composer && this.$refs.composer.focus());
        }
      }
    },
  }));
});
