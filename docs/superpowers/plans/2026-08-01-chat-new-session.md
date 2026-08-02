# Chat New Chat / Clear History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `New chat` button that hides the chat log's accumulated history from the current browser's view only, plus a `Xem toàn bộ lịch sử` control to bring it back — no database change, no data loss.

**Architecture:** A single new piece of Alpine state, `chatCutoff` (an ISO timestamp or `null`, persisted to `localStorage`), plus a `visibleMessages` getter that filters the existing `messages` array for display. Nothing about fetching, polling, or storing messages changes.

**Tech Stack:** Alpine.js (existing `app()` component in `static/js/app.js`), plain CSS, Flask/Jinja templates — no new dependency, no build step.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-01-chat-new-session-design.md` — this plan implements it in full.
- No backend change of any kind. No new endpoint, no new column, no migration.
- No JavaScript test framework — this project deliberately has none; verification is manual, in a real browser.
- The cutoff must be per-browser only (`localStorage`), never synced or sent to the server, and must be cleared on `logout()` so it can never hide history for a different user who logs into the same browser afterward.
- Vietnamese UI copy, matching the rest of the chat page.

---

### Task 1: Add the `chatCutoff` filter to the chat log

**Files:**
- Modify: `backend/src/network_copilot/static/js/app.js:46-51` (chat state block), `:53-64` (getters), `:151-179` (`logout()`)
- Modify: `backend/src/network_copilot/templates/index.html:56-58` (chat panel opening + the `x-for` on `messages`)
- Modify: `backend/src/network_copilot/static/css/app.css` (append new rules)

**Interfaces:**
- Consumes: the existing `messages` array (unchanged shape, `{id, username, role, content, payload, created_at, ...}`) already populated by `hydrateMessages()`/`pollMessages()`/`_ingestMessage()`.
- Produces: `chatCutoff` (state), `visibleMessages` (getter, `Array` of message objects), `hiddenMessageCount` (getter, `number`), `startNewChat()`, `showFullHistory()` — used only by `templates/index.html` in this task; no other task depends on them.

- [ ] **Step 1: Add `chatCutoff` state, initialized from `localStorage`**

In `backend/src/network_copilot/static/js/app.js`, find the existing chat state block (lines 46-51):

```javascript
    // -- chat --
    messages: [],
    draftMessage: "",
    sending: false,
    _messagesRefreshGeneration: 0,
    _clientMessageSequence: 0,
```

Replace with:

```javascript
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
```

- [ ] **Step 2: Add the `visibleMessages` and `hiddenMessageCount` getters**

Find the existing getters (lines 53-64):

```javascript
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
```

Add two new getters immediately after `pendingBatches`'s closing `},`:

```javascript
    get pendingBatches() {
      return Object.values(this.batchesById).filter(
        (batch) =>
          batch.status === "pending_approval" || batch.status === "approved"
      );
    },

    get visibleMessages() {
      if (!this.chatCutoff) return this.messages;
      // Reuse _messageTimestamp() (already defined further down in this
      // component) rather than comparing created_at strings directly: it
      // normalises server timestamps that arrive without a timezone
      // suffix, which a naive string comparison against an
      // always-suffixed chatCutoff (from toISOString()) would get wrong.
      const cutoffTime = Date.parse(this.chatCutoff);
      return this.messages.filter((message) => {
        const timestamp = this._messageTimestamp(message);
        return timestamp === null || timestamp > cutoffTime;
      });
    },

    get hiddenMessageCount() {
      return this.messages.length - this.visibleMessages.length;
    },
```

- [ ] **Step 3: Add `startNewChat()` and `showFullHistory()`**

Add these two methods anywhere in the component object — place them right after the `hiddenMessageCount` getter added in Step 2:

```javascript
    startNewChat() {
      this.chatCutoff = new Date().toISOString();
      localStorage.setItem("nc_chat_cutoff", this.chatCutoff);
    },

    showFullHistory() {
      this.chatCutoff = null;
      localStorage.removeItem("nc_chat_cutoff");
    },
```

- [ ] **Step 4: Clear the cutoff on logout**

Find this line inside `logout()` (around line 176-178):

```javascript
      this.messages = [];
      this.draftMessage = "";
      this.sending = false;
    },
```

Replace with:

```javascript
      this.messages = [];
      this.draftMessage = "";
      this.sending = false;
      this.chatCutoff = null;
      localStorage.removeItem("nc_chat_cutoff");
    },
```

- [ ] **Step 5: Add the toolbar (New chat button + hidden-history banner) and switch the chat log to `visibleMessages`**

In `backend/src/network_copilot/templates/index.html`, find:

```html
      <main class="chat-panel">
        <div class="chat-log" x-ref="chatLog">
          <template x-for="message in messages" :key="message.id">
```

Replace with:

```html
      <main class="chat-panel">
        <div class="chat-toolbar">
          <button type="button" class="new-chat-btn" @click="startNewChat()">
            New chat
          </button>
          <span class="chat-hidden-banner" x-show="chatCutoff" x-cloak>
            Đã ẩn <span x-text="hiddenMessageCount"></span> tin nhắn cũ ·
            <a href="#" @click.prevent="showFullHistory()">Xem toàn bộ lịch sử</a>
          </span>
        </div>
        <div class="chat-log" x-ref="chatLog">
          <template x-for="message in visibleMessages" :key="message.id">
```

- [ ] **Step 6: Append toolbar/banner CSS to `static/css/app.css`**

Add at the end of the file:

```css

/* -- Chat toolbar (New chat / hidden history) -- */

.chat-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  font-size: 12px;
}

.new-chat-btn {
  border: 1px solid var(--border);
  border-radius: 4px;
  background: transparent;
  color: var(--text);
  padding: 5px 10px;
  font-size: 12px;
  cursor: pointer;
}

.new-chat-btn:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.chat-hidden-banner {
  color: var(--text-muted);
}

.chat-hidden-banner a {
  color: var(--accent);
}
```

- [ ] **Step 7: Run the full backend test suite to confirm no regression**

No Python file changed in this task, but this confirms the working tree edit didn't break anything else.

Run: `../.venv/Scripts/python.exe -m pytest tests -q` (from `backend/`)
Expected: all tests pass (470 as of this plan; the count may have grown since, but none should fail)

- [ ] **Step 8: Manually verify in a real browser**

Start the backend (from `backend/`): `../.venv/Scripts/python.exe -m flask --app wsgi.py run` (or the `.claude/launch.json` "dashboard-check" preview config).

- Log in, send a couple of chat messages, click `New chat`. Confirm the chat log goes empty and a banner appears reading "Đã ẩn N tin nhắn cũ · Xem toàn bộ lịch sử" with the correct N.
- Send another message. Confirm it appears normally in the now-empty-looking log, below where the hidden history would be.
- Click `Xem toàn bộ lịch sử`. Confirm the full history (old + new messages, in the correct chronological order) reappears and the banner disappears.
- Refresh the page after clicking `New chat` again. Confirm the cutoff persists (log still starts empty on reload).
- Log out and log back in. Confirm the cutoff is gone and the full history shows again immediately.
- If practical, open the chat page in two different browser sessions logged in as two different users; confirm clicking `New chat` in one does not change what the other sees when it next polls/reloads.

- [ ] **Step 9: Commit**

```bash
git add backend/src/network_copilot/static/js/app.js backend/src/network_copilot/templates/index.html backend/src/network_copilot/static/css/app.css
git commit -m "feat: add New chat / clear history to the chat page"
```
