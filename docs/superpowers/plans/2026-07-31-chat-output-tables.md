# Chat Device Output Tables Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render device command output in the chat page's "operational results" panel as a table when the backend already supplies parsed, structured data (`result.parsed`), falling back to the existing raw `<pre>` block otherwise.

**Architecture:** Pure frontend change. Two small pure helper functions added to the existing `app()` Alpine component in `static/js/app.js`; the `operational-results` block in `templates/index.html` gains a conditional branch (table vs. `<pre>`); new CSS rules in `static/css/app.css` for the table, following the existing `.dashboard-table` pattern from the network dashboard feature.

**Tech Stack:** Alpine.js (already vendored, no new dependency), plain CSS, Flask/Jinja templates (unchanged server-side).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-31-chat-output-tables-design.md` — this plan implements it in full.
- No backend change of any kind — `result.parsed` already exists in the chat payload (`backend/src/network_copilot/ai/service.py:251`).
- No new JavaScript test framework — this project has none by deliberate choice; verification is manual, in a real browser.
- An empty `result.parsed` array (`[]`) must render the same as "no parser" (the existing `<pre>` fallback), not an empty table with no rows.
- Column headers are derived generically from `Object.keys(result.parsed[0])` — no per-command label lookup table.
- Vietnamese UI copy is not affected by this change (no new user-facing strings besides column headers, which are derived from English parser field names, matching the existing `.dashboard-table`/`.result-heading` precedent of English technical labels inside an otherwise-Vietnamese UI).

---

### Task 1: Render parsed command output as a table in the chat panel

**Files:**
- Modify: `backend/src/network_copilot/static/js/app.js:219-227` (insert helpers after `confirmHostnameMatches`)
- Modify: `backend/src/network_copilot/templates/index.html:93` (replace the single `<pre>` line)
- Modify: `backend/src/network_copilot/static/css/app.css` (append new rules)

**Interfaces:**
- Consumes: `result.parsed` — a chat message's `payload.results[i].parsed`, already sent by the backend (`list[dict] | None`, see `ai/service.py:251`; `None` when no parser is registered for that command, `[]` when the parser ran but matched no rows, otherwise a non-empty list of dicts with the same keys per row).
- Produces: `resultColumns(result) -> string[]`, `resultCellLabel(column) -> string`, `resultCellValue(row, column) -> string` on the `app()` Alpine component — used only by `templates/index.html` in this task; no other task depends on them.

- [ ] **Step 1: Add the three helper functions to `static/js/app.js`**

Find this existing block (`static/js/app.js:219-227`):

```javascript
      }
    },

    confirmHostnameMatches(id) {
      const change = this.changesById[id];
      const expected = change && change.device && change.device.hostname;
      if (!expected) return false;
      return (this.confirmInputs[id] || "").trim() === expected;
    },

    async applyChange(id) {
```

Insert the three new helpers right after `confirmHostnameMatches`'s closing `},` and before `async applyChange(id) {`:

```javascript
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
```

- [ ] **Step 2: Replace the `<pre>` block in `templates/index.html` with the table/`<pre>` branch**

Find this exact line (`templates/index.html:93`, inside the `result-row` div):

```html
                        <pre x-text="result.output || 'No output returned.'"></pre>
```

Replace it with:

```html
                        <template x-if="resultColumns(result).length === 0">
                          <pre x-text="result.output || 'No output returned.'"></pre>
                        </template>
                        <template x-if="resultColumns(result).length > 0">
                          <div class="result-table-wrap">
                            <table class="result-table">
                              <thead>
                                <tr>
                                  <template x-for="column in resultColumns(result)" :key="column">
                                    <th x-text="resultCellLabel(column)"></th>
                                  </template>
                                </tr>
                              </thead>
                              <tbody>
                                <template x-for="(row, rowIndex) in result.parsed" :key="rowIndex">
                                  <tr>
                                    <template x-for="column in resultColumns(result)" :key="column">
                                      <td x-text="resultCellValue(row, column)"></td>
                                    </template>
                                  </tr>
                                </template>
                              </tbody>
                            </table>
                          </div>
                        </template>
```

The surrounding `<div class="result-row">...</div>` and its `result-heading` block above this line are unchanged.

- [ ] **Step 3: Append the table CSS to `static/css/app.css`**

Add at the end of the file:

```css

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
  border-bottom: 1px solid #d9e0e6;
  white-space: nowrap;
}

.result-table th {
  color: #6b7a89;
  font-weight: 600;
  text-transform: uppercase;
  font-size: 10px;
}
```

- [ ] **Step 4: Run the full backend test suite to confirm no regression**

No Python file changed in this task, but this confirms the working tree edit didn't break anything else.

Run: `../.venv/Scripts/python.exe -m pytest tests -q` (from `backend/`)
Expected: all tests pass (467 as of this plan; the count may have grown since, but none should fail)

- [ ] **Step 5: Manually verify in a real browser**

Start the backend (from `backend/`): `../.venv/Scripts/python.exe -m flask --app wsgi.py run` (or use the project's `.claude/launch.json` "dashboard-check" preview config, which runs the same `wsgi.py`).

Log in, then in the chat input send messages that trigger each of the four parsed commands (adjust wording to match how the AI provider maps intents in this deployment — any phrasing that results in a monitor/troubleshoot action running the command works):
- A VLAN check on an access or distribution device (runs `show vlan brief`). Confirm a table renders with columns "Vlan Id", "Name", "Status", "Ports", and that a VLAN with more than one port shows them comma-joined in one cell.
- An OSPF neighbor check on a core or distribution device (runs `show ip ospf neighbor`). Confirm the neighbor table renders with columns "Neighbor Id", "Priority", "State", "Dead Time", "Address", "Interface".
- An interface check (runs `show ip interface brief`). Confirm a table renders with columns "Interface", "Ip Address", "Status", "Protocol".
- A routing table check (runs `show ip route`). Confirm a table renders with columns "Network", "Protocol", "Next Hop", "Interface", "Distance", "Metric", and that any row where `next_hop` or `interface` is null shows "—" in that cell. Confirm the table scrolls horizontally within its own row on a narrow browser window rather than widening the chat bubble.
- Trigger any command with no registered parser (e.g. ask a troubleshoot-intent question that runs `show running-config` or `show version`, if the AI provider routes to one). Confirm it still renders as the original `<pre>` block, unchanged.

- [ ] **Step 6: Commit**

```bash
git add backend/src/network_copilot/static/js/app.js backend/src/network_copilot/templates/index.html backend/src/network_copilot/static/css/app.css
git commit -m "feat: render parsed device command output as tables in chat"
```
