# Chat "New Chat" / Clear History — Design Spec

**Date:** 2026-08-01
**Status:** Approved for planning

## Goal

Let a user hide the chat log's accumulated history from their own view with
one click, without scrolling through it every time they send a new
message, and let them bring it back with one click when they need to look
something up. This affects only the clicking user's own browser — the
shared team chat log in the database, and what every other logged-in user
sees, is completely unaffected.

## Non-goals (explicitly out of scope for this iteration)

- Deleting anything from the database. `chat_messages` is untouched; this
  is a display filter only.
- Any backend change: no new endpoint, no new column, no migration.
- Syncing the "hidden" state across a user's different browsers/devices.
  The chosen scope (per-browser only, confirmed during brainstorming) means
  a `localStorage`-only approach, which is inherently local to one browser.
- Multiple named/persistent chat sessions to switch between (a full
  thread/session model). The user's actual need — not re-scrolling past
  old messages when sending a new one — is fully met by a single hide/show
  toggle; a multi-session model would be considerably more machinery for
  no added benefit here.
- Any change to `changesById`, the pending-changes sidebar, or the device
  list — none of them are affected by this filter.

## Architecture

Pure frontend change to the existing `app()` Alpine component
(`static/js/app.js`) and `templates/index.html`. A single piece of new
state, `chatCutoff` (an ISO timestamp string or `null`), drives a computed
getter that filters the already-fetched `messages` array for display.
Nothing about how messages are fetched, polled, or stored changes — `New
chat` and `Xem toàn bộ lịch sử` only change which of the already-loaded
messages are rendered.

## Behavior

- **`New chat` button** (in the chat panel, near the message input): sets
  `chatCutoff = new Date().toISOString()` and persists it to
  `localStorage` under `nc_chat_cutoff`. The chat log view immediately
  shows nothing (a fresh, empty-looking log) — new messages sent or
  received after this point appear normally.
- **Filtering:** a new getter, `visibleMessages`, returns `this.messages`
  unfiltered when `chatCutoff` is `null`, or filtered to
  `message.created_at > chatCutoff` when it is set. `templates/index.html`
  iterates over `visibleMessages` instead of `messages` in the chat log's
  `x-for`. Nothing else (polling, `_ingestMessage`, `_sortMessages`,
  `changesById`) changes — they keep operating on the full `messages`
  array as before.
- **Hidden-history banner:** whenever `chatCutoff` is set, a small bar
  above the chat log reads "Đã ẩn N tin nhắn cũ" (N = `messages.length -
  visibleMessages.length`) with a `Xem toàn bộ lịch sử` button.
- **`Xem toàn bộ lịch sử` button:** sets `chatCutoff = null` and removes
  `nc_chat_cutoff` from `localStorage`. The full history reappears
  immediately (no refetch needed — `messages` was never filtered, only the
  view was). Clicking `New chat` again afterward sets a fresh cutoff at
  that moment, same as the first time.
- **On logout:** `chatCutoff` is reset to `null` and `nc_chat_cutoff` is
  removed from `localStorage`, in the same place `logout()` already clears
  `devices`, `changesById`, and `messages`. This prevents one user's chosen
  cutoff from silently hiding history for a different user who logs into
  the same shared/lab machine afterward.
- **On page load:** `chatCutoff` initializes from `localStorage.getItem
  ("nc_chat_cutoff")`, the same pattern already used for `nc_token` and
  `nc_user`, so a cutoff set earlier survives a page refresh.

## Testing

No backend change, so no backend tests. No JavaScript test framework
exists in this project and none is introduced (consistent with every
prior frontend iteration this session).

**Manual verification in a real browser**, before this is reported
complete:
- Send a few chat messages, click `New chat` — confirm the log goes empty
  and the hidden-count banner appears with the correct count.
- Send a new message after clicking `New chat` — confirm it appears
  normally below the (now empty) hidden history.
- Click `Xem toàn bộ lịch sử` — confirm the full history (old + new
  messages) reappears, in the correct order, and the banner disappears.
- Refresh the page after setting a cutoff — confirm the cutoff persists
  (log still starts empty) via `localStorage`.
- Log out and log back in (or as a different user) — confirm the cutoff is
  gone and the full history shows again.
- Open the chat page in two different browsers/tabs logged in as two
  different users — confirm clicking `New chat` in one does not affect the
  other's view (this is definitional given the `localStorage`-only design,
  but worth a real confirmation since it's the core requirement from
  brainstorming).

## Rollout

Purely additive frontend change: no migration, no new dependency, no
backend edit. Deployment is the same as every prior frontend change:
`git pull` → restart the Flask process.
