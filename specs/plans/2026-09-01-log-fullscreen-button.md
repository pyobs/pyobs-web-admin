# Plan: fullscreen button for logs

Status: implemented, closed (#74, PR #83)

## Problem

The two log views (`templates/modules/all_logs.html`, fixed `height: 600px`;
`templates/modules/detail.html` `#tab-logs`, fixed `height: 520px`) render the log console at a
fixed height that's too small to follow a lively stream during an observing run or a long update.
There's no way to enlarge it short of browser zoom / DevTools. No fullscreen code exists anywhere
in the repo yet.

The log JS in the two templates is intentionally duplicated "in lockstep" (see the comment at the
top of the notifications block in `detail.html`) — no shared static JS/CSS file exists for logs —
so this must be added identically in both places.

## Design

Overlay-only approach (no native `Element.requestFullscreen()`): a CSS class toggle makes a
wrapper around the toolbar + status line + `<pre>` fill the viewport via
`position: fixed; inset: 0`. Chosen over the Fullscreen API because iOS Safari doesn't support
`requestFullscreen()` on arbitrary elements, and the overlay is simpler to implement/test with one
code path instead of two (native + fallback).

**Implementation note:** the class must go on a wrapper (`#log-container`) around the toolbar and
the `<pre>`, not on the `<pre>` alone — applying `position: fixed` to just the log box leaves the
toolbar (a sibling `<div>` above it) behind the fixed layer, hiding filter/refresh/acknowledge/etc.
entirely. Caught via a real browser check during implementation, not by DOM inspection alone (the
DOM computed styles look fine either way — this is a visual/z-stacking issue).

### 1. CSS (added to both templates — `extra_head` block)

`all_logs.html` has no `extra_head` block today; add one. `detail.html` already has one
(lines 5–13) — append there. Both templates wrap the toolbar + `#log-older-status` + `<pre>` in a
new `<div id="log-container">`.

```css
.log-fullscreen {
  position: fixed; inset: 0; z-index: 1046; /* above sidebar (1044/1045), mobile navbar (1043) */
  background: var(--pyobs-surface-bg);
  display: flex; flex-direction: column;
  padding: 1rem; margin: 0; overflow: auto;
}
.log-fullscreen #log-output { flex: 1 1 auto; height: auto !important; min-height: 0; }
```

`min-height: 0` on the flex child is required so the `<pre>` can shrink below its content height
and scroll internally instead of the whole overlay growing past the viewport.

Theme-aware via the existing `--pyobs-surface-bg` custom property (`templates/base.html:25-40`,
defined for both light and dark).

### 2. Toolbar button (both files)

```html
<button class="btn btn-outline-secondary" id="log-fullscreen-btn" onclick="toggleLogFullscreen()" title="Expand log to fullscreen" aria-pressed="false">
  <i class="bi bi-arrows-fullscreen"></i>
</button>
```

`aria-pressed` is kept in sync by `toggleLogFullscreen()` so screen-reader users get the toggle
state (the file's other icon-only buttons rely on `title` alone, but this one is a stateful
toggle, which is exactly the case `aria-pressed` is for).

- `all_logs.html`: in the toolbar div (~line 44-79), under the module checkboxes.
- `detail.html`: in the `#tab-logs` toolbar (~line 138 area), same relative position.

### 3. JS (added identically to both files, near the other log helpers)

```js
function toggleLogFullscreen() {
  const container = document.getElementById('log-container');
  const btn = document.getElementById('log-fullscreen-btn');
  const icon = btn.querySelector('i');
  const isFs = container.classList.toggle('log-fullscreen');
  icon.className = isFs ? 'bi bi-fullscreen-exit' : 'bi bi-arrows-fullscreen';
  btn.title = isFs ? 'Exit fullscreen' : 'Expand log to fullscreen';
  btn.setAttribute('aria-pressed', String(isFs));
  if (isFs) document.addEventListener('keydown', escExitLogFullscreen);
  else document.removeEventListener('keydown', escExitLogFullscreen);
}
function escExitLogFullscreen(e) {
  if (e.key === 'Escape') toggleLogFullscreen();
}
```

No `fullscreenchange`/`fullscreenerror` handling needed (overlay-only, no native API) — `Esc` is
handled via a manual keydown listener instead.

### 4. Interactions that must keep working — verified against current code, no changes needed

- Auto-refresh (`logTimer`, `setInterval(fetchLogs, 3000)` — `all_logs.html:455,532` /
  `detail.html:864,1017`): untouched, keeps running regardless of the DOM class.
- `renderLogs()`'s scroll-position preservation (`wasNearBottom` from `pre.scrollHeight` /
  `scrollTop` / `clientHeight`): unaffected — computed from the live element either way.
- Ack badge / `isNewIssue()` / click+Shift-click time-range on `.log-line` spans: all bound to
  `#log-output`, which is never replaced, only reclassed.
- `fetchOlderLogs()` scroll-to-top loader: listens on `#log-output` scroll; class toggle doesn't
  touch that listener.

### 5. Collapsed-state restoration

Free by construction: toggling `.log-fullscreen` off just removes the `position: fixed` override;
the inline `style="height: 600px/520px"` on the `<pre>` (`all_logs.html:90`, `detail.html:141`) is
never removed, so it re-applies automatically. Scroll position is preserved since it's the same
DOM node throughout (never detached/re-created).

## Acceptance criteria

- [ ] Fullscreen toggle button on both the All Logs page and the per-module Logs tab.
- [ ] Expanded log fills the whole viewport (no sidebar/navbar space eaten).
- [ ] Toolbar and all existing controls remain usable while expanded.
- [ ] Auto-refresh, NEW/ack badge, older-log loading on scroll-to-top, and click/Shift+click
      time-range still work while expanded.
- [ ] Exit via button and via `Esc`; icon state stays in sync.
- [ ] Collapsed state looks/behaves exactly as before (fixed heights restored after exit).
- [ ] Works in current Chrome, Firefox, Safari, and iOS Safari (overlay works everywhere since it
      doesn't depend on the Fullscreen API).
- [ ] Both templates updated in lockstep (same behavior, same helper code).

## Out of scope

- Native Fullscreen API (`requestFullscreen()`) — overlay-only per the design decision above.
- Keyboard shortcut (e.g. `f`) to toggle fullscreen — optional nicety in the issue, not required.
- Packages page transient operation-log panel (`#update-panel-log` in
  `templates/modules/packages.html`) — different kind of log, explicitly out of scope in #74.

## Related

- #44 (browser notifications for log WARNING+) — shares the per-module Logs tab / All Logs page;
  keep the notification flow working while expanded.
- #59 (logs for both config name and comm name) — touches the same log plumbing.
