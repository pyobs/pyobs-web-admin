# Plan: "Update all" button on the Packages page

Status: implemented

Landed: PR #81 ("Add \"Update all\" bulk action to the Packages page", commits `0129b78`/`fafba81`,
merged `afea89b`) — the "Update all" button, `updateAllPackages()`'s sequential queue, and the
`bulkRunning`/`awaitUpdateCompletion` guards on `templates/modules/packages.html`. No backend
changes, per the proposal below. Closes #79.

Related: builds on `specs/plans/2026-08-16-async-package-update.md` (the background-job/lock
design below is consumed as-is, not changed).

## Problem

The Packages page (`templates/modules/packages.html`) updates one `pyobs-*` package at a time via
its per-row Update/Reinstall button. #79 asks for a bulk "update all" action.

This can't be a simple bulk-fire like Dashboard's `controlAll`/`controlOutdated`
(`templates/modules/dashboard.html:325-374`), which fires staggered requests with a fixed 1s
`sleep` and never waits for completion. Package updates are serialized host-wide by design:
`update_package_start` (`modules/services.py:874`) takes an flock and refuses a second job while
one is running (see the async-package-update plan's "one job at a time, host-wide" rationale —
concurrent `pip install` against the same venv races on `dist-info`/`RECORD`). A staggered-fire
loop would have every request after the first come back `{"ok": false, "message": "Already
updating X"}`. "Update all" has to actually wait for each job to reach a terminal state before
starting the next.

## Proposal

1. New "Update all (N)" button next to Refresh on the Packages page, mirroring the
   `btn-restart-outdated` count-badge pattern on Dashboard. **Per-host**, following the page's
   existing session-active-host model — no fleet-wide version, consistent with this app's
   established stance that bulk actions are a footgun outside a single host's own page
   (README.md, `specs/design/index.md:76-88`).
2. Eligibility = whatever `pkgRow()` already renders as a non-disabled Update/Reinstall button
   (`packages.html:93`) — outdated regular packages, plus **vcs "Reinstall" packages** (per
   confirmed scope: include vcs, not just PyPI-version-bump packages). No new eligibility logic;
   reuse the existing per-row `disabled` computation.
3. Client-side sequential queue: POST `.../update/` for package *i*, await its terminal state via
   the existing status endpoint, then move to *i+1*. No new backend endpoint and no server-side
   queue/job-list — the existing single-job lock file is reused exactly as today, one name at a
   time.
4. **Continue past failures** ("install as much as possible"): a `failed` or `interrupted` result
   for one package does not stop the queue — log it and proceed to the next. End with a per-package
   outcome summary (updated / failed / interrupted / skipped-start), not just whatever the last
   package's raw pip log happened to say.
5. Guard against overlap with a manual single-package click (or a resumed in-flight job from
   another tab/admin) racing the queue and hitting the server-side lock.

## Implementation

### 1. Extract an awaitable poll helper — `packages.html`

`pollUpdateStatus()` today is fire-and-forget (`setTimeout` recursion, used for the single-click
flow and `resumeUpdateIfActive`). Add `awaitUpdateCompletion()`: same 1.5s-interval poll against
`api_package_update_status`, still calling `renderUpdateLog(status)` on every tick for live output,
but as an `async` loop that `return`s the final status once `!status.active` instead of scheduling
another `setTimeout`. Keep the existing `pollUpdateStatus` for the single-click path unchanged
(same auto-hide-panel-on-success behavior); both it and the new queue function call the same
underlying fetch/render step so the polling logic isn't duplicated.

### 2. `updateAllPackages()` — `packages.html`

```
async function updateAllPackages() {
  const btn = document.getElementById('btn-update-all');
  const queue = [...document.querySelectorAll('#packages-tbody tr[data-package]')]
    .filter(row => !row.querySelector('button').disabled)
    .map(row => row.dataset.package);
  if (!queue.length) return;

  setBulkControlsDisabled(true);   // btn-update-all + every row's Update/Reinstall + btn-refresh
  const outcomes = [];
  for (let i = 0; i < queue.length; i++) {
    const name = queue[i];
    showQueueProgress(i + 1, queue.length, name);   // "Updating 3/8: pyobs-fli…"
    const resp = await fetch(`/api/packages/${encodeURIComponent(name)}/update/`, {
      method: 'POST', headers: {'X-CSRFToken': getCsrfToken()},
    });
    const data = await resp.json();
    if (!data.ok) {
      outcomes.push({name, state: 'not-started', detail: data.message || data.error});
      continue;                      // per-#5: lock contention on this one -- move on regardless
    }
    const status = await awaitUpdateCompletion();
    outcomes.push({name, state: status.state});
  }
  renderQueueSummary(outcomes);      // replaces the log panel with a per-package result list
  setBulkControlsDisabled(false);
  await loadPackages();
}
```

Queue membership is captured once, up front, from the currently-rendered DOM — not re-derived
mid-loop — so it can't be affected by `loadPackages()` re-renders triggered elsewhere (e.g. a
concurrent Refresh click, which is why `btn-refresh` is disabled for the duration too).

### 3. Guard against overlap

- `setBulkControlsDisabled(true)` disables `btn-update-all`, `btn-refresh`, and every row's action
  button before the loop starts — prevents a manual click during the queue from ever reaching the
  server and getting a confusing "Already updating X" response.
- `resumeUpdateIfActive()` (`packages.html:169-182`, runs on page load) already detects a job
  in flight from *before* this page load (e.g. another admin/tab, or a page refresh mid-queue).
  Extend it: if `status.active` on load, also disable `btn-update-all` until that job reaches a
  terminal state (reuse `awaitUpdateCompletion()` then re-enable), so a second admin can't start a
  queue on top of a job already running.
- Queue itself never needs this guard mid-run — it always awaits terminal state before its own
  next POST — so no client-side retry/backoff logic is needed for the "Already updating" case
  during a queue's own iterations, only for something external racing it.

### 4. UI additions — `packages.html`

- `<button id="btn-update-all">` next to `btn-refresh`, same style as `btn-restart-outdated`
  (count badge, `disabled` when count is 0).
- Recompute the count inside `loadPackages()`, after rendering rows, from
  `[...tbody.querySelectorAll('button')].filter(b => !b.disabled).length` — no new eligibility
  logic, this is exactly what already drives each row's own disabled state.
- `update-panel` gets a progress line during a queue run ("Updating 3/8: pyobs-fli…") instead of
  the single-package title; on completion, its body is replaced by a per-package outcome list
  (name → updated/failed/interrupted/not-started) rather than the last package's raw log, since
  that log is no longer representative of the whole run. The raw live pip log for whichever
  package is currently running still streams in `update-panel-log` while that package is active.

### 5. No backend changes

`api_packages`, `api_package_update`, `api_package_update_status`, and the one-job-at-a-time flock
(`modules/services.py:798-928`) are untouched. "Install as much as possible" and the per-host scope
both fall out of client-side sequencing over the existing per-package endpoints — no new job-state
file, no new endpoint.

### 6. Tests

No new backend surface, so no new `modules/tests.py` coverage from this plan. Manual verification
instead: a host with 2+ outdated packages (mix of PyPI and at least one vcs-installed), click
Update all, confirm via the log panel *and* `pkg-update.json` that only one `pip install` is ever
in flight at a time; force one package to fail (e.g. temporarily point it at a bad version spec)
and confirm the queue continues to the remaining packages and the summary reports the failure
correctly; refresh the page mid-queue and confirm `resumeUpdateIfActive` picks the in-flight job
back up sanely rather than the queue silently vanishing.

### 7. Docs

- README.md Packages section: mention the "Update all" bulk button and its per-host scope.
- `specs/plans/index.md`: add this plan's entry, mark implemented once shipped.
