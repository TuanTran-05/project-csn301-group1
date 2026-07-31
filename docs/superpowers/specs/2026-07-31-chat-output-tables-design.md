# Chat Device Output Tables — Design Spec

**Date:** 2026-07-31
**Status:** Approved for planning

## Goal

Make device command output shown in the chat page easier to scan. Today,
every command result in the "operational results" panel (the output of a
monitor/troubleshoot AI action) renders as a raw `<pre>` block of CLI text —
readable but not easy to scan for a VLAN table or an OSPF neighbor list.
`ai/service.py` already computes a parsed, structured form of this output
(`result.parsed`, via the existing `parsers` module) and sends it to the
frontend; the frontend currently ignores that field. This spec renders it
as a table when available.

This is a small, frontend-only, cohesive change: one rendering decision
(table vs. raw text) applied to one existing panel.

## Non-goals (explicitly out of scope for this iteration)

- Any backend change. `result.parsed` already exists in the payload
  (`ai/service.py:251`); nothing server-side needs to change.
- The "Final verification" section of the change action card. Its
  `verification_output` entries carry `passed`/`output`/`details` only, no
  `parsed` field — adding one is a backend change and a separate iteration.
- Syntax highlighting or generic table-detection for commands with no
  registered parser (`network_copilot/parsers/__init__.py::PARSERS` covers
  exactly `show ip interface brief`, `show vlan brief`, `show ip route`,
  `show ip ospf neighbor`). Anything else keeps rendering as `<pre>`, exactly
  as today.
- A per-command column-label mapping. Column headers are derived
  generically from the parsed dict's keys (see Frontend design) rather than
  a maintained lookup table — the exact tradeoff discussed and accepted
  during design.
- Sorting, filtering, or pagination of the rendered table.

## Architecture

Pure frontend change, no migration, no new dependency:

- `static/js/app.js`: two small helper functions, no new state.
- `templates/index.html`: the existing `operational-results` block gains a
  conditional branch — table when `result.parsed` is a non-empty array,
  otherwise the current `<pre>` (unchanged).
- `static/css/app.css`: new rules for the table, following the existing
  `.dashboard-table` pattern already used on `/dashboard`.

## Frontend design

### Helper functions (`static/js/app.js`)

```javascript
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
```

`resultColumns` returning `[]` is what the template uses to decide table vs.
`<pre>` — an empty `parsed` array (a command that ran but matched no rows,
e.g. zero OSPF neighbors) is treated the same as "no parser" and falls back
to the raw text, since there is no row to derive column headers from. This
matches current behavior for that case (the raw CLI output, e.g. just a
header line, is still shown as text).

### Template change (`templates/index.html`)

Inside the existing `result-row` loop (the block rendering `message.payload.results`),
replace the unconditional `<pre>` with a column-vs-table branch:

```html
<div class="result-row">
  <div class="result-heading">
    <code x-text="result.command"></code>
    <span class="result-status" :class="'result-' + result.status" x-text="result.status"></span>
    <span class="result-duration" x-text="result.duration_ms != null ? result.duration_ms + ' ms' : ''"></span>
  </div>
  <template x-if="resultColumns(result).length === 0">
    <pre x-text="result.output || 'No output returned.'"></pre>
  </template>
  <template x-if="resultColumns(result).length > 0">
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
  </template>
</div>
```

This replaces the current unconditional `<pre x-text="result.output || 'No output returned.'"></pre>`
line inside the same loop, one-for-one.

### CSS (`static/css/app.css`)

New rules, following the existing `.dashboard-table` convention:

```css
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

.result-table-wrap {
  overflow-x: auto;
}
```

The table's parent (`.result-row`) already has no horizontal-scroll
container; a table with many columns (e.g. `show ip route`, 6 columns) on a
narrow viewport needs `overflow-x: auto` — wrap the `<table>` in
`<div class="result-table-wrap">...</div>` so it scrolls independently
instead of widening the whole bubble.

## Testing

No backend change, so no backend test changes. No JavaScript test framework
exists in this project (consistent with every prior frontend iteration) and
none is introduced here.

**Manual verification in a real browser**, before this is reported
complete:
- Send a monitor-intent chat message that runs `show vlan brief` on an
  access/distribution device — confirm a table renders with columns
  Vlan Id / Name / Status / Ports, and that a VLAN with multiple ports
  shows them comma-joined.
- Send one for `show ip ospf neighbor` on a core/distribution device —
  confirm the neighbor table renders.
- Send one for `show ip interface brief` and one for `show ip route` —
  confirm both render as tables, including rows where `next_hop` or
  `interface` is `null` (rendered as "—").
- Send a monitor/troubleshoot request for a command with no registered
  parser (e.g. `show running-config`, if reachable through the AI's
  troubleshoot intent) — confirm it still renders as `<pre>`, unchanged
  from current behavior.
- Confirm a `show ip route` table (6 columns) does not widen the chat
  bubble on a narrow window — it should scroll horizontally within its own
  row instead.

## Rollout

Purely additive frontend change: no migration, no new dependency, no
backend edit. Deployment is the same as every prior frontend change on the
AI Server node: `git pull` → restart the Flask process (static files are
served directly, no build step).
