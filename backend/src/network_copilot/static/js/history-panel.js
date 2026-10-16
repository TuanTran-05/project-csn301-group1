/* Nested Alpine component; inherited authFetch remains untouched. */
document.addEventListener("alpine:init", () => {
  Alpine.data("historyDraft", () => ({
    draftSnapshot: null,
    historyPanel: null,
    historyApiBase: "/api/pending-history",
    pendingEditor: null,
    pendingEditorError: "",
    _historyTimer: null,
    _historyRefreshScheduled: false,
    get pendingHistoryRevision() {
      return JSON.stringify([this.changesById || {}, this.batchesById || {}]);
    },
    init() {
      this.$watch("token", () => this.connectHistory());
      this.$watch("currentUser", () => this.connectHistory());
      this.$watch("pendingHistoryRevision", () => this.scheduleHistoryRefresh());
      this.connectHistory();
    },
    scheduleHistoryRefresh() {
      if (this._historyRefreshScheduled) return;
      this._historyRefreshScheduled = true;
      Promise.resolve().then(() => {
        this._historyRefreshScheduled = false;
        this.historyPanel?.refresh().catch(() => {});
      });
    },
    connectHistory() {
      clearInterval(this._historyTimer);
      this._historyTimer = null;
      this.historyPanel?.destroy();
      this.historyPanel = null;
      this.draftSnapshot = null;
      this.pendingEditor = null;
      this._pendingEditorOpening = null;
      if (!this.token || this.currentUser?.role !== "ADMIN") return;
      const token = this.token;
      const isCurrentSession = () => this.token === token && this.currentUser?.role === "ADMIN";
      this.historyPanel = NetAIHistory.mount(this.$el, {
        apiBase: this.historyApiBase,
        request: async (url, options) => {
          if (!isCurrentSession()) throw new Error("Phiên đăng nhập đã thay đổi.");
          if (this.historyApiBase === "/api/pending-history" && options?.method &&
              Object.keys(this.batchActionIds || {}).length) {
            throw new Error("Đợi thao tác batch hiện tại hoàn tất rồi thử lại.");
          }
          const result = await this.authFetch(url, options);
          if (!isCurrentSession()) throw new Error("Phiên đăng nhập đã thay đổi.");
          return result;
        },
        onSnapshot: async (snapshot) => {
          if (snapshot?.schema === "pending-v1") {
            // Invalidate outstanding GETs, then use the core's canonical refresh paths.
            this._changesRefreshGeneration += 1;
            this._batchesRefreshGeneration += 1;
            this._batchesRefreshRequestToken += 1;
            await Promise.all([this.refreshChanges(), this.refreshBatches()]);
            if (!isCurrentSession()) return;
          }
          this.draftSnapshot = snapshot;
          this.$dispatch("nc-history-snapshot", { snapshot });
        },
      });
      this.historyPanel.refresh().catch(() => {});
      this._historyTimer = setInterval(() => {
        this.historyPanel?.refresh().catch(() => {});
      }, 15000);
    },
    async openPendingEditor(kind, id) {
      const row = kind === "batch" ? this.batchesById[id] : this.changesById[id];
      if (!row || row.status !== "pending_approval" || row.requested_by_id !== this.currentUser?.id) return;
      this.pendingEditorError = "";
      this.pendingEditor = null;
      const token = this.token;
      const opening = this._pendingEditorOpening = {};
      try {
        const response = await this.authFetch(`${this.historyApiBase}/${kind === "batch" ? "batches" : "changes"}/${id}/editor`);
        if (this.token !== token || this._pendingEditorOpening !== opening) return;
        this.pendingEditor = {
          kind, id, revision: response.revision, description: response.draft.description || "",
          changes: response.draft.changes.map(change => ({
            id: change.id, hostname: change.hostname,
            description: change.description || "", commandsText: (change.commands || []).join("\n"),
            execution_mode: change.execution_mode,
          })),
        };
      } catch (error) {
        if (this.token === token && this._pendingEditorOpening === opening) this.pendingEditorError = error.message;
      }
    },
    async savePendingEditor() {
      if (!this.pendingEditor || !this.historyPanel) return;
      const editor = this.pendingEditor;
      const patch = child => ({ description: child.description, execution_mode: child.execution_mode,
        commands: child.commandsText.split("\n").map(line => line.trim()).filter(Boolean) });
      const payload = editor.kind === "batch" ? { description: editor.description,
        changes: editor.changes.map(child => ({ id: child.id, ...patch(child) })) } : patch(editor.changes[0]);
      payload.revision = editor.revision;
      try {
        await this.historyPanel.edit(editor.kind, editor.id, payload);
        if (this.pendingEditor === editor) this.pendingEditor = null;
      } catch (error) {
        if (this.pendingEditor === editor) this.pendingEditorError = error.message;
      }
    },
    destroy() {
      clearInterval(this._historyTimer);
      this.historyPanel?.destroy();
    },
  }));
});
