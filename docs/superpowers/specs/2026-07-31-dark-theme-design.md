# Dark NOC Theme — Design Spec

**Date:** 2026-07-31
**Status:** Approved for planning

## Goal

Replace the app's current light theme with a dark, "network operations
console" aesthetic across every page that shares `static/css/app.css`: the
login screen, the chat page (`/`), and the dashboard (`/dashboard`). The
domain is continuous network monitoring — a dark background with glowing
status indicators is both a better fit for a screen left open for long
stretches and, per the user's own selection during a visual brainstorming
session, the preferred direction over a refined-light or light-with-accent
alternative.

## Non-goals (explicitly out of scope for this iteration)

- A light/dark toggle. This replaces the light theme outright; the user
  explicitly chose "replace entirely" over "add a toggle" to avoid
  maintaining and testing two parallel palettes.
- Any HTML structure change. No new elements, no new classes, no changes to
  `templates/index.html` or `templates/dashboard.html`.
- Any JavaScript change. `app.js` and `dashboard.js` are untouched.
- Any backend change.
- A CSS framework, preprocessor, or build step. The project deliberately
  has none (see the original chat UI spec's non-goals); this stays plain
  CSS edited directly in `static/css/app.css`.

## Palette

Selected via the visual brainstorming companion (Dark NOC Console
direction, cyan accent):

| Token | Value | Used for |
|---|---|---|
| `--bg` | `#0e1826` | Page background |
| `--surface` | `#131f30` | Panel/card backgrounds (sidebar, chat panel, action-card, dashboard panels, login form) |
| `--surface-raised` | `#0b1420` | Top bar (slightly darker than surface, matching the mockup) |
| `--border` | `#223349` | Borders between panels, table rows, dividers |
| `--text` | `#dbe4ee` | Primary text |
| `--text-muted` | `#6f8299` | Labels, secondary/meta text, timestamps |
| `--accent` | `#06b6d4` | Primary buttons, role badge, links, focus states, nav-link border |
| `--accent-contrast` | `#04222b` | Text/icon color rendered on top of `--accent` (dark, for contrast against a light cyan) |
| `--online` | `#22c55e` | Online status dot/pill (with a subtle glow: small `box-shadow` in the same color) |
| `--offline` | `#ef4444` | Offline status dot/pill (same glow treatment) |
| `--unknown` | `--text-muted` | Unknown status dot/pill (no glow) |
| `--danger-bg` | `#3a1616` | Backgrounds for error text, dangerous-command confirmation box, failed/blocked result rows |
| `--danger-text` | `#f3a6a6` | Text on `--danger-bg` |
| `--success-bg` | `#122f1e` | Backgrounds for passed verification, success result rows |
| `--success-text` | `#7ee2a8` | Text on `--success-bg` |
| `--warn-bg` | `#3a2e12` | Background for "degraded" health pill and medium-risk pill |
| `--warn-text` | `#f0c674` | Text on `--warn-bg` |

These are defined once as CSS custom properties in a `:root` block at the
top of `static/css/app.css` and referenced everywhere a color value
currently appears as a literal hex code. Every rule in the file that sets a
`color`, `background`/`background-color`, or `border-color` is in scope for
this substitution — this is a full pass over the file, not a partial one,
since a partial pass is exactly how a light-background rule gets missed and
ends up looking broken against the new dark surroundings.

## Component-specific notes

- **Status dots and pills** (`.status-dot`, `.status-online/offline/unknown`,
  `.health-pill` and its `ok/degraded/down/no_data` modifiers, `.risk-pill`,
  `.status-pill`, `.result-status` and its `success/failed/blocked`
  modifiers): today these use light pastel backgrounds (`#dcefe3`,
  `#fdecea`, `#fdf1d6`, etc.) with dark text — the classic light-mode
  "badge" look. On a dark surface those same pastels would look like bright
  flashbulbs. They're restyled to dark-tinted backgrounds
  (`--danger-bg`/`--success-bg`/`--warn-bg`) with light, saturated text
  (`--danger-text`/`--success-text`/`--warn-text`), keeping each status's
  color identity (red = bad, green = good, amber = caution) while fitting
  the dark surface.
- **Buttons** (`.login-form button`, `.chat-input button`,
  `.sidebar-actions button`, `.action-card-buttons button`,
  `.logout-btn`): background becomes `--accent`, text becomes
  `--accent-contrast` for buttons that were previously solid blue-on-white;
  the outlined `.logout-btn` keeps a border style but in `--border`/`--text`
  tones instead of the current light-on-dark-blue-header combination (the
  top bar is already dark today, so this one changes least).
- **Inputs** (`.login-form input`, `.chat-input input`,
  `.confirm-hostname input`): background `--surface`, border `--border`,
  text `--text`, placeholder text uses `--text-muted`.
- **`<pre>` blocks and `.result-table`**: background shifts from the
  current light gray (`#f4f6f8`) to a slightly darker-than-surface tone so
  code/table content still reads as a distinct "output" region against the
  panel it sits in — reuse `--bg` for this (it's darker than `--surface`).
- **`.bubble-user` / `.bubble-assistant` / `.bubble-system`**: `bubble-user`
  keeps using `--accent`/`--accent-contrast` (it's the same "primary
  action color" role blue played before); `bubble-assistant` uses
  `--surface`; `bubble-system` uses `--danger-bg`/`--danger-text` (it is
  used for blocked/error system messages today, so this preserves meaning).

## Testing

No automated test exists for CSS-only presentation and none is introduced
(consistent with the project's established no-JS-test-framework stance).

**Manual verification in a real browser**, before this is reported complete:
- Login screen: form, inputs, button, error text all readable against the
  dark background.
- Chat page: device sidebar (all three status colors), a monitor-result
  bubble with the result table from the previous feature, an action-card in
  each of its states (pending approval, approved with the dangerous-command
  confirmation box visible, applied with verification results shown), and
  the pending-changes sidebar.
- Dashboard page: all four panels (device rollup table, OSPF health pills
  and neighbor list, changes lists, audit feed), including the `no_data`
  OSPF hint text.
- Spot-check contrast: no dark-gray-on-dark-background or light-pastel
  leftovers anywhere in the pages above — this is the specific failure mode
  a full-file color pass is meant to prevent, so the check exists to catch
  anything the pass missed.

## Rollout

Purely additive/replacing within one CSS file: no migration, no new
dependency, no backend or template edit. Deployment is the same as every
prior frontend change: `git pull` → restart the Flask process.
