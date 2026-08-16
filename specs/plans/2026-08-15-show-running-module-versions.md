# Plan: Show the pyobs-* versions each running module loaded, flag outdated ones, restart them

Status: implemented

Related: `specs/plans/2026-08-15-log-loaded-pyobs-package-versions.md` in **pyobs-core** — this plan
consumes the startup log line that one adds. Without that pyobs-core change there is nothing to
read; modules started before it ships will simply report "unknown".

## Problem

pyobs-web-admin can already show a module's status, PID, uptime, CPU/memory (`get_module_stats`,
`modules/services.py:892`) and, per host, the *installed* pyobs-* package versions (`list_pyobs_packages`,
the Packages page / fleet Overview matrix). It cannot show which versions a specific module is
*actually running* right now — the versions it imported when it started, which may lag the
installed set after an upgrade. And it has no way to see, at a glance, which running modules are
behind and to restart just those.

pyobs-core now logs that at startup:

```
2026-08-15 12:00:00 [INFO] (camera) application.py:379 Loaded pyobs packages: pyobs-core=2.0.0.dev76, pyobs-fli=2.0.0.dev7
```

web-admin already reads a module's log (flat file or journald), so it can extract this without any
comm/XMPP involvement — which also makes it work for comm-less modules like `httpfilecache`.

## Proposal

1. Add `get_module_versions(name) -> dict[str, str] | None` to read the newest `Loaded pyobs
   packages:` line from a module's log.
2. Compare that against the installed set (`list_pyobs_packages`) and flag a module as **outdated**
   when any of its loaded packages differs from the installed version.
3. Surface it on the **Dashboard** (per active host): a per-row "outdated" badge showing which
   packages lag, plus a **Restart outdated** button that restarts only those running modules.
4. Also show the running versions on the module detail **Overview** tab (running versions, and an
   "outdated" marker where applicable).

Deliberately **not** on the fleet Overview page: it has no per-module actions by design (a
fleet-wide "Stop All" is a documented footgun), and a fleet-wide "restart all outdated" would be
exactly that. Restart stays a per-host action, like every other bulk button.

The comparison is running-vs-*installed*, not running-vs-latest-PyPI — the Packages page already
covers installed-vs-latest. "Outdated" here means "upgraded but not restarted".

## Implementation

### 1. `get_module_versions(name)` — `modules/services.py`

Returns `{distribution: version}` from the newest `Loaded pyobs packages:` line, or `None` if the
module has none (started before the pyobs-core change, or the line has rotated out of retention).

- **Parse** is backend-agnostic: locate `"Loaded pyobs packages: "` in the line, take the remainder,
  split on `", "`, then each `k=v` on `"="`. Versions are semver/dev strings with no `,` or `=`, and
  distribution names are `pyobs-*` with no `=` either, so a plain split is safe.
- **file backend**: `tac <logfile> | grep -m1 "Loaded pyobs packages:"` — `tac` reads from the end
  so the newest occurrence is found without scanning the whole (potentially large, rotated) file.
  Log file path is `_log_dir() / f"{_active_name(name)}.log"`, same as `get_logs`.
- **journald backend**: reuse `_journalctl_json(["SYSLOG_IDENTIFIER=pyobs",
  f"PYOBS_MODULE={_journald_module_tag(name)}", "--grep", "Loaded pyobs packages:", "-o", "json",
  "--no-pager"])`, take the last entry, and match on its `MESSAGE`. The journal `MESSAGE` already
  carries the `"<module> <file>:<line> Loaded pyobs packages: ..."` shape (pyobs's journal
  formatter), so the same substring parse applies. Bound the search to the module's own lifetime
  with `--since @<create_time>` (`psutil.Process(pid).create_time()`, already used by
  `get_module_stats`) — otherwise `--grep` scans the entire retained journal, and for a long-running
  module the version line is old.

### 2. Cache by PID — `modules/services.py`

The version set is fixed for the lifetime of a process, so it must not be re-shelled-out on every
status poll. Cache it keyed by module name → `(pid, versions)`, mirroring the existing
`_process_cache` pattern (`services.py:888`): read the PID via `_read_pid(name)`; if the cached PID
matches, return the cached result; otherwise recompute and store. A stopped module (no PID) returns
`None` and clears the entry.

### 3. Compare — `stale_packages(running, installed)` — `modules/services.py`

Pure helper returning the sorted list of package names where `running[dist] != installed[dist]`
(`installed` is `{p["name"]: p["version"] for p in list_pyobs_packages()}`). Distribution names
match across the two sources (`pyobs-fli` from `pip list` and from `importlib.metadata`). String
inequality is the first cut; a PEP 440 `Version` comparison (`running < installed`) can be layered
on later if downgrades ever need to be distinguished from upgrades.

### 4. Expose it — `modules/views.py`, `api_all_statuses` + `api_status`

- `api_status` (`views.py:438`) adds `"versions"` (the running dict, or `None`) and `"outdated"`
  (list of stale package names, or `None`) alongside `{"status", "stats"}`. It's host-routed via
  `_proxy`, so hub hosts get it too, once they run a build that includes it.
- `api_all_statuses` (`views.py:424`) adds the same two fields per module. To avoid a fresh
  `pip list` on every 10 s dashboard poll, fetch the installed set once per request (or cache it
  with a short TTL mirroring `_pyobs_core_version_cache`), then compare each running module's
  cached `get_module_versions` result against it. The per-module `get_module_versions` calls are
  PID-cached, so the only recurring cost is one `pip list` per poll — acceptable, and cacheable if
  it ever isn't.

### 5. Render — `templates/modules/dashboard.html`

- Per row: when `outdated` is non-empty, show a warning badge next to the status dot (title lists
  `running → installed` per package, e.g. `pyobs-core 2.0.0.dev41 → 2.0.0.dev76`).
- Add a **Restart outdated** button next to Start/Restart/Stop All, reusing `controlAll`'s staggered
  logic but filtered to modules that are running, not deactivated, and flagged outdated. Disabled
  with a count of zero. It should fall back to a no-op (and refresh) when nothing is outdated.
- Wire the comparison into `refreshAllStatuses()` (the 10 s poll), which already receives each
  module's status/stats and drives the row state.

### 6. Render — `templates/modules/detail.html`

Add a `stat-versions-row` to the Overview table (next to PID/Uptime/Memory/CPU), populated in
`updateStatus()` (`detail.html:276`): list each `name=version` (compact
`pyobs-core 2.0.0.dev76, pyobs-fli 2.0.0.dev7`), with an "outdated" marker where the pair differs
from installed; show "unknown" when absent while running.

## Tests

In `modules/tests.py`:

- `get_module_versions` file backend: parses the newest of several `Loaded pyobs packages:` lines
  from a temp log file; returns `None` when absent.
- `get_module_versions` journald backend: a captured `journalctl -o json` fixture whose `MESSAGE`
  carries the line, asserted to parse correctly; `--grep`/`--since` arg order asserted like the
  existing journald log tests.
- Cache: second call with the same PID does not re-run `subprocess`; a changed PID recomputes.
- `stale_packages`: flags only differing packages; empty list when all match; handles a package
  missing from one side (treats as differing / skip — decide in code).
- `api_status`/`api_all_statuses`: returns `versions`/`outdated` when running, `None` when stopped
  (mocking `get_module_versions` and `list_pyobs_packages`).

## Consequences

- **Good:** answers "which versions is this module actually running" per module, comm-independent,
  working for `httpfilecache` and any other comm-less module, and gives one click to restart exactly
  the modules that need it.
- **Good:** the PID cache makes the per-module read one `tac`/`journalctl` per process lifetime, so
  the 10 s status poll stays cheap; the only recurring cost is one `pip list` per poll.
- **Neutral:** relies on the pyobs-core log line having been shipped; older-started modules read
  "unknown" until restarted. That's expected, not a bug.
- **Risk:** "Restart outdated" is still a real action (camera/roof restart) — no worse than the
  existing unguarded Restart All, but it keeps the 1 s stagger and skips deactivated modules.
- **Cache in memory, not the DB.** The version set is fixed per PID, so a time-based TTL (e.g.
  10 s) would just re-query forever; keying on the PID re-queries only on a module restart. A DB
  cache would only save the one-time warm-up after a web-admin restart, at the cost of SQLite
  writes from request handlers and a PID-keyed invalidation problem (stale rows the instant a
  module restarts) — not worth it. This mirrors the existing in-memory cache pattern
  (`_process_cache` at `services.py:888`, `_pyobs_core_version_cache`).
