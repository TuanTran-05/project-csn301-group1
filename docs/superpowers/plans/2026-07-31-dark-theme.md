# Dark NOC Theme Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `static/css/app.css`'s light theme with the dark NOC palette chosen during brainstorming (dark background, cyan accent), covering every page that shares this stylesheet (login, chat, dashboard), with no HTML/JS/backend changes.

**Architecture:** A single full-file rewrite of `static/css/app.css`: a new `:root` custom-property block defines the palette once, and every existing rule that currently sets a color/background/border-color literal is rewritten to reference those properties (or a semantic-status property like `--success-bg`) instead. Structural properties (layout, spacing, typography sizing, flex/grid rules) are carried over unchanged.

**Tech Stack:** Plain CSS (custom properties), no build step, no new dependency.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-31-dark-theme-design.md` — this plan implements it in full.
- No HTML change (`templates/index.html`, `templates/dashboard.html` untouched) and no JS change (`static/js/app.js`, `static/js/dashboard.js` untouched). Only `static/css/app.css` is modified.
- No backend change.
- No CSS framework, preprocessor, or build tooling — the file stays plain, hand-edited CSS.
- One correction from the spec, caught during planning: the spec's "Risk/health pill" note implies both `.risk-pill`/`.status-pill` and `.health-pill` get per-level color coding. Only `.health-pill` actually has level-specific modifier classes in the codebase today (`.health-ok`, `.health-degraded`, `.health-down`, `.health-no_data`, driven by `:class="'health-' + entry.health"` in `dashboard.html`). `.risk-pill` and `.status-pill` are rendered with a single static class regardless of the risk level or status text (`<span class="risk-pill" x-text="change.risk_level"></span>` — no `:class` binding), so adding per-level colors to them would require an HTML template change, which is out of scope (see Global Constraints above and the spec's Non-goals). This plan re-colors `.risk-pill`/`.status-pill` as a single flat neutral badge for the dark background, and gives the full tri-color (`ok`/`degraded`/`down`) treatment only to `.health-pill`, which already has the modifier classes to support it.

---

### Task 1: Rewrite `static/css/app.css` with the dark NOC palette

**Files:**
- Modify: `backend/src/network_copilot/static/css/app.css` (complete file rewrite — every rule, no line-range diff, since this is a full color pass by design)

**Interfaces:**
- Consumes: nothing — pure CSS, no dependency on other tasks.
- Produces: nothing consumed by another task — this plan has exactly one task.

- [ ] **Step 1: Replace the entire contents of `static/css/app.css`**

Replace the whole file with:

```css
:root {
  --bg: #0e1826;
  --surface: #131f30;
  --surface-raised: #0b1420;
  --border: #223349;
  --text: #dbe4ee;
  --text-muted: #6f8299;
  --accent: #06b6d4;
  --accent-contrast: #04222b;
  --online: #22c55e;
  --offline: #ef4444;
  --unknown: #6f8299;
  --danger-bg: #3a1616;
  --danger-text: #f3a6a6;
  --success-bg: #122f1e;
  --success-text: #7ee2a8;
  --warn-bg: #3a2e12;
  --warn-text: #f0c674;
}

[x-cloak] { display: none !important; }

* { box-sizing: border-box; }

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

body {
  margin: 0;
  font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
}

/* -- Login screen -- */

.login-screen {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
}

.login-form {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 32px;
  width: 320px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.login-form h1 {
  margin: 0 0 8px;
  font-size: 20px;
}

.login-form label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  color: var(--text-muted);
}

.login-form input {
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 4px;
  font-size: 14px;
  background: var(--surface);
  color: var(--text);
}

.login-form button {
  margin-top: 8px;
  padding: 10px;
  border: none;
  border-radius: 4px;
  background: var(--accent);
  color: var(--accent-contrast);
  font-size: 14px;
  cursor: pointer;
}

.error-text {
  color: var(--danger-text);
  font-size: 13px;
  margin: 0;
}

/* -- App shell -- */

.app-shell {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

.top-bar {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 12px 20px;
  background: var(--surface-raised);
  color: var(--text);
}

.top-bar .app-title {
  font-weight: 600;
  margin-right: auto;
}

.user-badge {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}

.role-pill {
  background: var(--accent);
  color: var(--accent-contrast);
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 11px;
  text-transform: uppercase;
}

.logout-btn {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text);
  padding: 6px 12px;
  border-radius: 4px;
  cursor: pointer;
}

.main-grid {
  flex: 1;
  display: grid;
  grid-template-columns: 220px minmax(0, 1fr) 260px;
  gap: 12px;
  padding: 12px;
  min-height: 0;
}

@media (max-width: 900px) {
  .main-grid {
    grid-template-columns: 1fr;
  }
}

.sidebar {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 12px;
  overflow-y: auto;
}

.sidebar h2 {
  font-size: 13px;
  text-transform: uppercase;
  color: var(--text-muted);
  margin: 0 0 10px;
}

.sidebar ul {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.sidebar li {
  font-size: 13px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 8px;
}

.status-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 6px;
}
.status-online { background: var(--online); box-shadow: 0 0 6px var(--online); }
.status-offline { background: var(--offline); box-shadow: 0 0 6px var(--offline); }
.status-unknown { background: var(--unknown); }

.device-role {
  color: var(--text-muted);
  font-size: 11px;
  margin-left: 6px;
}

.device-status {
  display: block;
  color: var(--text-muted);
  font-size: 11px;
  margin: 3px 0 0 14px;
  text-transform: capitalize;
}

.change-command-summary {
  color: var(--text-muted);
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 11px;
  line-height: 1.4;
  margin: 4px 0 6px;
  overflow-wrap: anywhere;
}

.sidebar-actions {
  margin-top: 6px;
  display: flex;
  gap: 6px;
}

.sidebar-actions button,
.action-card-buttons button {
  border: none;
  border-radius: 4px;
  padding: 5px 10px;
  font-size: 12px;
  cursor: pointer;
  background: var(--accent);
  color: var(--accent-contrast);
}

/* -- Chat -- */

.chat-panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.chat-log {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.bubble {
  max-width: 70%;
  padding: 10px 12px;
  border-radius: 8px;
  font-size: 14px;
}

.bubble-user {
  align-self: flex-end;
  background: var(--accent);
  color: var(--accent-contrast);
}

.bubble-assistant {
  align-self: flex-start;
  background: var(--border);
}

.bubble-system {
  align-self: center;
  background: var(--danger-bg);
  color: var(--danger-text);
  font-size: 13px;
}

.bubble-pending {
  opacity: 0.6;
  font-style: italic;
}

.bubble-meta {
  font-size: 11px;
  opacity: 0.7;
  margin-bottom: 4px;
  display: flex;
  gap: 8px;
}

.bubble-content {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.operational-results {
  border-top: 1px solid var(--border);
  margin-top: 10px;
  padding-top: 8px;
}

.operational-heading,
.result-heading {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}

.operational-heading {
  font-size: 12px;
}

.operational-label {
  color: var(--text-muted);
  display: block;
  font-size: 10px;
  font-weight: 600;
  margin-bottom: 4px;
  text-transform: uppercase;
}

.operational-heading .operational-label {
  margin: 0;
}

.intent-pill,
.result-status {
  background: var(--border);
  border-radius: 10px;
  color: var(--text);
  font-size: 10px;
  padding: 2px 7px;
  text-transform: uppercase;
}

.intent-pill {
  margin-left: auto;
}

.operational-analysis {
  border-left: 2px solid var(--text-muted);
  color: var(--text);
  font-size: 12px;
  line-height: 1.45;
  margin: 8px 0 0;
  padding-left: 8px;
  white-space: pre-wrap;
}

.result-list,
.verification-results {
  margin-top: 8px;
}

.result-row,
.verification-row {
  border-top: 1px solid var(--border);
  padding: 7px 0;
}

.result-heading code {
  font-size: 11px;
  overflow-wrap: anywhere;
}

.result-duration {
  color: var(--text-muted);
  font-size: 10px;
  margin-left: auto;
}

.result-success {
  background: var(--success-bg);
  color: var(--success-text);
}

.result-failed,
.result-blocked {
  background: var(--danger-bg);
  color: var(--danger-text);
}

.result-row pre,
.verification-row pre {
  background: var(--bg);
  border-radius: 4px;
  font-size: 11px;
  margin: 6px 0 0;
  max-height: 220px;
  overflow: auto;
  padding: 7px;
  white-space: pre-wrap;
}

.action-card {
  margin-top: 10px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px;
  color: var(--text);
}

.action-card-header {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
  font-size: 12px;
}

.action-card pre {
  background: var(--bg);
  margin: 4px 0 0;
  padding: 8px;
  border-radius: 4px;
  font-size: 12px;
  overflow-x: auto;
}

.change-target {
  font-size: 12px;
  font-weight: 600;
  margin-bottom: 8px;
}

.change-detail {
  margin-top: 8px;
}

.change-error {
  background: var(--danger-bg);
  border-left: 2px solid var(--offline);
  color: var(--danger-text);
  font-size: 12px;
  margin-top: 8px;
  padding: 7px 8px;
  white-space: pre-wrap;
}

.verification-details {
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.4;
  margin-top: 5px;
}

.action-card-buttons {
  display: flex;
  gap: 6px;
  margin-top: 8px;
}

.action-card-buttons button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.confirm-hostname {
  background: var(--danger-bg);
  border-left: 2px solid var(--offline);
  border-radius: 4px;
  margin-top: 8px;
  padding: 8px;
}

.confirm-hostname-warning {
  color: var(--danger-text);
  font-size: 12px;
  line-height: 1.4;
  margin: 0 0 6px;
}

.confirm-hostname input {
  border: 1px solid var(--border);
  border-radius: 4px;
  box-sizing: border-box;
  font-size: 13px;
  padding: 6px 8px;
  width: 100%;
  background: var(--surface);
  color: var(--text);
}

.risk-pill, .status-pill {
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 11px;
  background: var(--border);
  color: var(--text);
}

.chat-input {
  display: flex;
  gap: 8px;
  padding: 12px;
  border-top: 1px solid var(--border);
}

.chat-input input {
  flex: 1;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: 4px;
  font-size: 14px;
  background: var(--surface);
  color: var(--text);
}

.chat-input button {
  padding: 10px 18px;
  border: none;
  border-radius: 4px;
  background: var(--accent);
  color: var(--accent-contrast);
  cursor: pointer;
}

.chat-input button:disabled,
.chat-input input:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

/* -- Dashboard -- */

.nav-link {
  color: var(--accent);
  text-decoration: none;
  font-size: 13px;
  border: 1px solid var(--accent);
  padding: 6px 12px;
  border-radius: 4px;
}

.nav-link:hover {
  background: rgba(255, 255, 255, 0.08);
}

.dashboard-error {
  background: var(--danger-bg);
  color: var(--danger-text);
  font-size: 13px;
  margin: 0;
  padding: 8px 20px;
}

.dashboard-grid {
  flex: 1;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  padding: 12px;
}

@media (max-width: 900px) {
  .dashboard-grid {
    grid-template-columns: 1fr;
  }
}

.dashboard-panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 14px;
  overflow-y: auto;
}

.dashboard-panel h2 {
  font-size: 14px;
  margin: 0 0 10px;
}

.dashboard-panel h3 {
  font-size: 12px;
  color: var(--text-muted);
  text-transform: uppercase;
  margin: 12px 0 6px;
}

.dashboard-hint {
  color: var(--text-muted);
  font-size: 12px;
}

.dashboard-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.dashboard-table th,
.dashboard-table td {
  text-align: left;
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
}

.dashboard-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
}

.dashboard-list a {
  display: flex;
  gap: 8px;
  align-items: center;
  color: inherit;
  text-decoration: none;
}

.dashboard-list a:hover {
  text-decoration: underline;
}

.ospf-entry {
  border-top: 1px solid var(--border);
  padding: 8px 0;
}

.ospf-entry-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}

.ospf-count {
  color: var(--text-muted);
  font-size: 11px;
  margin-left: auto;
}

.health-pill {
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 11px;
  text-transform: uppercase;
}

.health-ok { background: var(--success-bg); color: var(--success-text); }
.health-degraded { background: var(--warn-bg); color: var(--warn-text); }
.health-down, .health-no_data { background: var(--danger-bg); color: var(--danger-text); }

.ospf-neighbor-list {
  list-style: none;
  margin: 6px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 11px;
  color: var(--text-muted);
}

.ospf-neighbor-list li {
  display: flex;
  gap: 8px;
}

.audit-time {
  color: var(--text-muted);
  font-size: 11px;
  min-width: 70px;
}

/* -- Chat result tables -- */

.result-table-wrap {
  overflow-x: auto;
}

.result-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11px;
  margin-top: 6px;
}

.result-table th,
.result-table td {
  text-align: left;
  padding: 4px 6px;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}

.result-table th {
  color: var(--text-muted);
  font-weight: 600;
  text-transform: uppercase;
  font-size: 10px;
}
```

- [ ] **Step 2: Run the full backend test suite to confirm no regression**

No Python file changed in this task, but this confirms the working tree edit didn't break anything else (matches the pattern used for every prior CSS-only change this session).

Run: `../.venv/Scripts/python.exe -m pytest tests -q` (from `backend/`)
Expected: all tests pass (467 as of this plan; the count may have grown since, but none should fail)

- [ ] **Step 3: Manually verify in a real browser**

Start the backend (from `backend/`): `../.venv/Scripts/python.exe -m flask --app wsgi.py run` (or the `.claude/launch.json` "dashboard-check" preview config).

- Login screen: confirm the form, inputs, and button are all clearly readable against the dark background — no white-on-white or dark-on-dark text.
- Chat page (`/`): confirm the device sidebar shows all three status-dot colors correctly (with a visible glow on online/offline), a monitor-result message renders its result table (from the previous feature) readably, an action-card shows correctly in each state — pending approval, approved with the dangerous-command confirmation box visible and legible, and applied with the verification results section shown — and the pending-changes sidebar list is readable.
- Dashboard page (`/dashboard`): confirm all four panels render readably — the device role rollup table, the OSPF panel (including at least one health-pill in each color it can take: `ok`, `degraded`/`down`/`no_data` — synthetic data is fine if the real lab only shows one health state), the changes lists, and the audit feed.
- Confirm the "Chat" / "Dashboard" nav links are visible against the dark top bar.
- Spot-check for any remaining light-background or unreadable-contrast element across all three pages — this is the specific failure mode a full-file pass is meant to prevent.

- [ ] **Step 4: Commit**

```bash
git add backend/src/network_copilot/static/css/app.css
git commit -m "feat: apply the dark NOC theme across the app"
```
