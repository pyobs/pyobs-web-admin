# Plan: Flag running modules as needing restart after their config file changes

Status: implemented

Related: GitHub issue #89.

## Problem

pyobs modules load their YAML config once at startup and never hot-reload it. Nothing in
web-admin tracks whether a module's on-disk config has drifted from what the running process
actually loaded, so there's no visible signal that a restart is needed after the config changed
underneath it — whether from a single-file edit (`save_config()`) or a `git pull`/`git reset`
that can touch many modules' files at once.

## Proposal

Reuse the existing package-version-drift shape (`stale_packages()` / `get_module_versions()` /
"Restart outdated") rather than inventing a new one, per the issue's own suggestion — but with a
simpler mechanism than "diff on every write path".

**Snapshot at start, compare at poll time**, mirroring how `stale_packages()` itself works (it
never hooks the `pip install` path — it just compares running-vs-installed lazily): hash the
config file's content when `start_module()` confirms the process is up, store the hash next to
the PID file, and compare it against the file's current hash on each status poll.

This sidesteps the issue's own open question ("for a `git pull` that touches N modules... flag
individually or batch") for free: since staleness is evaluated per module at poll time rather than
diffed at write time, `git_pull()`/`save_shared_config()`/any other write path needs zero new
code — each affected module just shows up stale on the next poll, individually, including one
whose only change came in via a `{include}`d shared fragment (which a write-path hook on
`save_config()` alone would miss).

**Trade-off accepted:** this hashes the file from the outside at spawn time, not what the process
actually parsed after `{include}` resolution — same proxy relationship `get_module_stats()` and
`get_module_versions()` already have to "what's really happening inside the process". Write and
spawn are effectively atomic here (`save_config()` finishes before anyone restarts), so this
should never produce a false negative in practice; not treated as a design risk.

## Implementation

### 1. Snapshot on start — `modules/services.py`

- `_config_hash_file(name) -> Path`: `_run_dir() / f"{_active_name(name)}.config.sha256"`,
  parallel to `_pid_file()` (`services.py:964`) — same active-name convention, so a module
  manually started while deactivated (`_camera`) hashes under the same key its PID/log files
  already use.
- `_hash_config(path: Path) -> str`: `hashlib.sha256(path.read_bytes()).hexdigest()`.
- In `start_module()` (`services.py:1001`), once the PID-confirmation loop finds the process
  alive (the existing `return True, f"Started {name} (PID {pid})"` branch), write
  `_hash_config(config_file)` to `_config_hash_file(name)` before returning. `restart_module()`
  gets this for free since it just calls `start_module()`.
- No hook needed anywhere else — not `save_config()`, not `save_shared_config()`, not
  `git_pull()`/`git_reset()`/`git_clone()`. That is the point of the snapshot-and-compare design.

### 2. Compare — `config_stale(name) -> bool | None` — `modules/services.py`

```python
def config_stale(name: str) -> bool | None:
    snap_file = _config_hash_file(name)
    if not snap_file.exists():
        return None  # never snapshotted -- started before this shipped, or an old snapshot
                      # that predates a since-deleted run dir; unknown, not stale
    config_file = _config_dir() / f"{name}.yaml"
    if not config_file.exists():
        return None
    try:
        snapshot = snap_file.read_text().strip()
    except OSError:
        return None
    return snapshot != _hash_config(config_file)
```

`None` (unknown) is deliberately distinct from `False` (confirmed fresh) — same three-state shape
`get_module_versions()` already has for "no data yet" vs. a real answer, so the UI can render
"unknown" instead of silently claiming freshness for a module that was simply never snapshotted.

### 3. Expose it — `modules/views.py`, `api_all_statuses` + `api_status`

- `api_status` (`views.py:472`) adds `"config_stale"` (`bool | None`) alongside the existing
  `"versions"`/`"outdated"`, computed only when `status == "running"` (`None` otherwise) —
  same guard `_versions_and_outdated()` already applies.
- `api_all_statuses` (`views.py:451`) adds the same field per module. `config_stale()` is two
  stat calls (snapshot file + config file) per running module per poll — cheap enough to not need
  the caching `get_module_versions()`/`list_pyobs_packages()` require; no new cache layer.

### 4. Render — `templates/modules/dashboard.html`

- A second per-row indicator icon next to `outdated-indicator` (`dashboard.html:105`):
  `config-stale-indicator`, shown when `m.status === 'running' && m.config_stale === true`,
  tooltip "Config changed since this module started — restart needed".
- A second bulk button next to **Restart outdated** (`dashboard.html:18`): **Restart stale
  config (N)**, wired the same way `controlOutdated()` (`dashboard.html:361`) is — staggered
  restart filtered to `row.dataset.configStale === '1'` and not deactivated.
- `refreshAllStatuses()` (`dashboard.html:235`) gains the `config_stale` branch alongside the
  existing `outdated` one: toggles the indicator, sets `row.dataset.configStale`, maintains a
  `config-stale-count` counter, and enables/disables the new button.

### 5. Render — `templates/modules/detail.html`

- A `stat-config-row` in the Overview table (`detail.html:87`, next to `stat-versions-row`):
  "up to date" / "changed since start — restart needed" / "unknown", driven by `data.config_stale`
  in `updateStatus()` (`detail.html:289`), same `d-none` show/hide pattern the versions row uses.

## Tests

In `modules/tests.py`:

- `start_module()` snapshotting: after a successful start, `_config_hash_file(name)` exists and
  its content is the sha256 of the config file written at that point (extend
  `StartModuleLogBackendTests` or a sibling class with its same temp-dir fixture).
- `config_stale()`: no snapshot file → `None`; snapshot matches current content → `False`;
  content changed after snapshot → `True`; config file deleted after snapshot → `None`.
- `api_status`/`api_all_statuses`: `config_stale` present (`True`/`False`/`None`) when running,
  `None` when stopped — mirrors the existing `versions`/`outdated` assertions in the
  `api_status`/`api_all_statuses` test class (~`tests.py:3184`).

## Consequences

- **Good:** resolves the issue's "individually or batch" open question by construction — no
  batching logic needed, and it also covers shared-fragment (`{include}`) edits for free, which
  the issue's own "hook `save_config()`" proposal would have missed.
- **Good:** zero new hooks on any config-write path; the only new state is one small file per
  running module, written at the one point (`start_module()`) that already writes a PID file.
- **Neutral:** like `get_module_versions()`, a module started before this ships (or whose run dir
  was cleared) reads "unknown" until its next restart — expected, not a bug.
- **Risk:** none beyond what "Restart outdated" already carries — same restart action, same
  staggering, same skip-deactivated-modules guard.
