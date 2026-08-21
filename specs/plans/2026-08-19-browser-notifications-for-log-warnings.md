# Plan: Browser notifications for log warnings and errors

Status: implemented, closed (PR #61, merged 2026-08-20; #44 closed)

Issues: #44

## Problem

When the All Logs tab (or the per-module Logs tab) is open with auto-refresh enabled, new log
entries at WARNING level or higher get a red "NEW" badge in-place — but only if the admin is
actually looking at the tab. If they have switched away (or the tab is backgrounded), a warning
or error can pile up unseen until they happen to come back. There is no out-of-band signal that
something needs attention.

## Proposal

Use the browser `Notification` API: each `fetchLogs` cycle detects *truly new* WARNING/ERROR/
CRITICAL lines that arrived since the previous fetch and fires a notification (module name,
level, truncated message) when the tab is not visible. A single global on/off toggle on the All
Logs page controls it everywhere; the preference is persisted to `localStorage` alongside the
existing log settings.

Both log windows already carry every hook this needs — `parseLogLevel()`/`LEVEL_ORDER`,
`isNewIssue()`, per-module `log-ack-*` timestamps, `mergeWithOlderHistory()`, and the
auto-refresh toggle + localStorage pattern (see `templates/modules/all_logs.html`,
`templates/modules/detail.html`, kept in lockstep as this repo already does for these two
near-identical log windows). No backend or API change is required — the fetch response contains
everything needed; this is a pure frontend change to the two templates' script blocks.

## Design decisions (settled)

- **"Truly new" detection = not in the pre-merge buffer.** `fetchLogs` currently replaces
  `rawLogLines` via `mergeWithOlderHistory()` (splicing the fresh tail onto older history).
  The notifier runs *before* that merge: snapshot the current `rawLogLines` as a `Set`, diff the
  incoming `data.lines` against it, and only the lines absent from the snapshot are candidates.
  This is the same exact-line-content dedup key `fetchOlderLogs` already uses; a known caveat
  applies identically here — two genuinely distinct lines that are byte-identical (e.g. the
  same recurring error re-logged within the same timestamp second) collapse to one candidate.
  Acceptable: it only ever *under*-notifies by one duplicate, never fabricates a notification.
- **Ack-aware: only notify lines that would badge as new.** A line that is new *this fetch* but
  whose timestamp is ≤ the module's `log-ack-<module>` instant (i.e. the admin already
  acknowledged that window, or it predates the first-view baseline) must not notify — exactly
  the `isNewIssue()` predicate the "NEW" badge uses. Combining the two: candidate =
  `new this fetch AND isNewIssue(line)`. This is what makes the per-module ack timestamps
  meaningful across refreshes, per the issue's requirement.
- **Coalesce per module per fetch cycle, not one notification per line.** The issue's literal
  wording ("for each new line … fire a browser notification") would fire 50 OS notifications in
  a row during an error burst — exactly the situation this feature exists to surface. Settled:
  at most one notification per module per cycle, body listing the worst level and the count
  (e.g. "camera: 3 new ERROR lines since last refresh — first: camera.py:42 …"). Deviates from
  the issue's per-line wording; rationale: notifications are for attention, not a log stream —
  the All Logs page itself remains the stream.
- **Notify only when the document is hidden.** The motivation is "the user is not looking at
  the tab" — while they are watching, the red "NEW" badge and the ack button already do the
  job, and a notification popping up mid-watch is noise. `document.hidden` (true when another
  tab is focused or the window is minimized) is checked at the moment candidates are found.
  The toggle still gates everything; a "notify even while watching" knob is deliberately not
  added (not requested; easy follow-up if anyone wants it).
- **One global toggle, default off.** A `form-switch` labeled "Browser notifications" in the
  All Logs toolbar next to "Auto-refresh", persisted under `localStorage['log-notifications']`
  (default `false` — intrusive/attention-seeking features are opt-in, unlike auto-refresh).
  The detail.html Logs tab reads the same key: no separate toggle there (the issue asks for
  one settings switch, on the All Logs page), but the emission logic runs on both pages so the
  per-module view behaves identically once enabled.
- **Permission flow.** First time the toggle is turned on, request `Notification.requestPermission()`
  if `Notification.permission === 'default'`. If it ends up `'denied'`, keep the toggle on but
  show a small note next to it ("Notifications blocked — allow them in browser settings")
  instead of silently doing nothing. If the API is unavailable (very old browser, insecure
  context — `Notification` requires HTTPS or localhost), hide the toggle entirely.

## Implementation

### 1. `templates/modules/all_logs.html` and `templates/modules/detail.html` (in lockstep)

- **Markup**: All Logs toolbar gains the "Browser notifications" `form-switch` (id
  `notifications-toggle`) next to the auto-refresh switch, plus a tiny muted note span for the
  permission-denied case. detail.html needs no new markup — it shares the preference and only
  runs the emission logic.
- **Helpers** (script block, after the existing `isNewIssue`/`lineModule` definitions):
  - `notificationsEnabled()` — `localStorage.getItem('log-notifications') === 'true'`
    (plus `'Notification' in window` guard).
  - `notifyMessage(line)` — strips the `TS [LEVEL] ([host]) (module) file:line ` prefix,
    truncates the remainder to ~120 chars, returns `{module, level, text}`; `lineModule()` and
    `parseLogLevel()` already handle the optional `[host]` tag and level extraction.
  - `emitLogNotifications(newLines)` — the coalescing core:
    1. Bail if disabled, permission not `'granted'`, or `!document.hidden`.
    2. `newLines.filter(isNewIssue)` — ack-aware candidates.
    3. Group by `lineModule()`; per module keep the highest level and a count; build one
       `new Notification(title, {body, tag})` per module. `tag` = `log-notify-<module>` so a
       browser replaces an older notification for the same module instead of stacking them.
- **Wiring**: inside `fetchLogs`, before `rawLogLines = mergeWithOlderHistory(...)`:
  `emitLogNotifications((data.lines || []).filter(l => !new Set(rawLogLines).has(l)))`.
  (The `Set` is built per cycle — the buffers are ≤ a few thousand lines, and this runs at
  most once per 3s poll, so no caching is warranted.)
- **Toggle handler** (All Logs only): persist the checkbox to `localStorage`; when turning on,
  request permission if `Notification.permission === 'default'`; after the (possibly async)
  result, show/hide the denied note. On load, restore the checkbox from storage and reflect
  the current permission state.
- **detail.html**: only the helper + wiring changes; the toggle lives on All Logs. Since both
  pages share `log-ack-<module>` keys and `isNewIssue` semantics, per-module notifications are
  acked the same way from either page.

## Tests

No JS test harness exists in this repo, so verification is manual, following the existing
convention for the log windows:

1. All Logs with auto-refresh on, toggle on, notification permission granted → move focus to
   another tab; append a WARNING/ERROR/CRITICAL line to a module's log; a notification for that
   module appears (title/body show module, level, truncated message).
2. Same, while the admin tab is focused → no notification (badge still appears).
3. A burst of N same-level errors in one cycle → exactly one notification, body says "N new
   ERROR lines".
4. Acknowledge, then refresh → no re-notification for the acknowledged window; a later new
   warning still notifies.
5. Toggle off → nothing notifies from either page; preference survives a page reload.
6. Permission denied → note appears next to the toggle; no `Notification` calls (no
   console errors).
7. detail.html Logs tab behaves identically once the toggle is on (shares the key).
8. Regression: log rendering, "NEW" badges, ack badge count, auto-refresh, and
   scroll-to-top paging all unchanged.

## Consequences

- **Good:** out-of-band attention for the exact case the issue names (user away from the tab),
  on both log windows, with a single opt-in switch and no backend changes.
- **Neutral:** notification availability depends on browser support and a secure context
  (HTTPS/localhost); the feature degrades to "toggle hidden" where unavailable.
- **Accepted trade-off:** byte-identical same-second duplicate lines under-notify by one; error
  bursts collapse to one notification per module per cycle — both in favor of not spamming the
  OS notification center.
