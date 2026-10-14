/* Explicit adapter: do not intercept Alpine state, polling or device operations. */
window.NetAIHistory = {
  mount(root, { request, onSnapshot, topologyId = "default", apiBase = "/api/draft-history" }) {
    if (typeof root === "string") root = document.querySelector(root);
    if (!root || typeof request !== "function" || typeof onSnapshot !== "function") {
      throw new Error("History toolbar needs a root, request and draft onSnapshot adapter");
    }
    const undo = root.querySelector('[data-history-action="undo"]');
    const redo = root.querySelector('[data-history-action="redo"]');
    const message = root.querySelector(".nc-history-message");
    if (!undo || !redo || !message) throw new Error("History toolbar markup missing");
    let state = { can_undo: false, can_redo: false }, busy = false, destroyed = false;
    const base = apiBase;
    const url = (suffix = "") => `${base}${suffix}?topology_id=${encodeURIComponent(topologyId)}`;
    function render() {
      if (destroyed) return;
      undo.disabled = busy || destroyed || !state.can_undo;
      redo.disabled = busy || destroyed || !state.can_redo;
      root.querySelector(".nc-history-toolbar").setAttribute("aria-busy", String(busy));
    }
    async function run(suffix, options, apply = true) {
      if (destroyed) throw new Error("History toolbar has been destroyed");
      if (busy) throw new Error("History operation in progress");
      busy = true; render(); message.hidden = true;
      try {
        const next = await request(url(suffix), options);
        if (destroyed) return;
        state = next;
        if (apply) await onSnapshot(next.snapshot);
        return next;
      } catch (error) {
        if (!destroyed) {
          // Unknown server outcome: disable until caller refreshes authoritative state.
          state = { can_undo: false, can_redo: false };
          message.textContent = error.message || "Không thể cập nhật lịch sử cấu hình.";
          message.hidden = false;
        }
        throw error;
      } finally { busy = false; render(); }
    }
    const post = (data) => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
    const onUndo = () => {
      if (busy || destroyed || !state.can_undo) return;
      return run("/undo", { method: "POST" }).catch(() => {});
    };
    const onRedo = () => {
      if (busy || destroyed || !state.can_redo) return;
      return run("/redo", { method: "POST" }).catch(() => {});
    };
    undo.addEventListener("click", onUndo); redo.addEventListener("click", onRedo); render();
    return {
      refresh: () => run("", undefined),
      push: (snapshot, actionType, description) => run("/states", post({ snapshot, action_type: actionType, description })),
      edit: (kind, id, draft) => run(`/${kind === "batch" ? "batches" : "changes"}/${id}`, { ...post(draft), method: "PATCH" }),
      restore: (backup) => run("/restore", post(typeof backup === "string" ? JSON.parse(backup) : backup)),
      async exportBackup() {
        if (destroyed) throw new Error("History toolbar has been destroyed");
        const backup = await request(url("/backup"));
        if (destroyed) throw new Error("History toolbar has been destroyed");
        return backup;
      },
      destroy() {
        if (destroyed) return;
        state = { can_undo: false, can_redo: false }; busy = false; render();
        message.hidden = true; message.textContent = "";
        destroyed = true;
        undo.removeEventListener("click", onUndo); redo.removeEventListener("click", onRedo);
      },
    };
  },
};
