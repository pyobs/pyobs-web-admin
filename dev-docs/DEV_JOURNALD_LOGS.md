# pyobs-web-admin: journald-backed module logging — v1.3 (2026-07-21)

## Status

**Implemented and verified live end-to-end** (Work Plan items 1–4; see Progress log), including
the previously-open group-less service-account case — see v1.3 in Progress log and the updated
Cross-user journal read permission section. No open items remain.
v1.1 adds auto-detection: `PYOBS_LOG_BACKEND`'s default changed from `"file"` to `None`
(auto-detect from `pyobsd`'s own config file), not a Work Plan item originally but a real gap
closed after `pyobsd` (`pyobs-core`'s daemon manager) turned out to read its own global
config for the same `file`-vs-`journald` decision — see Progress log. v1.2 adds pagination:
the log windows (module `Logs` tab, fleet-wide All Logs) now auto-load older entries when
scrolled to the top, for journald-backed modules only — the file backend reports "nothing
older available" rather than pretending to page further back, since a plain `tail -n` has no
seek/offset to page with — see Progress log and Design's "Pagination: load older logs".

## Progress log

- **Done.** `PYOBS_LOG_BACKEND` setting added to `pyobs_web_admin/settings.py`
  (`"file"` default / `"journald"`). `start_module()` branches on it — `--syslog` instead of
  `--log-file` for `"journald"`, nothing else changes. `get_logs()`/`get_log_stats()` gained
  journald branches (`modules/services.py`: `_journalctl_json`, `_journal_entry_to_line`,
  `_get_logs_journald`, `_get_log_stats_journald`), reconstructing the exact file-backend line
  shape so `_LOG_LEVEL_RE`/`_TS_RE`/`filter_str`/templates need zero changes. Unit tests
  (`StartModuleLogBackendTests`, `LogBackendJournaldTests` in `modules/tests.py`) use real
  `journalctl -o json` fixtures captured during this doc's own live verification, not invented
  shapes — `python manage.py test modules`, 71/71 passing.
- **A real bug, caught only by live testing, not by the fixtures above.** The first live
  end-to-end run (a real `pyobs.modules.Module` instance, started via `--syslog`, read back
  through `get_logs`) produced lines with the file:line info doubled — e.g. `... (testmod)
  /home/.../pyobs/application.py:155 testmod application.py:155 Loading configuration...`.
  Root cause: `logging_journald`'s `CODE_FILE` field is `record.pathname` (a full path), but
  pyobs's own journal formatter builds `MESSAGE`'s `"<module> <file>:<line> "` prefix from
  `%(filename)s` (just the basename) — so `_journal_entry_to_line`'s prefix-stripping,
  originally built from `CODE_FILE` directly, never matched and left the prefix in place. The
  unit test fixtures above didn't catch this because they were captured from an earlier
  synthetic test that passed a bare `"camera.py"` as the record's filename (already equal to
  its own basename) rather than a real call site's full path. Fixed by `os.path.basename()`-ing
  `CODE_FILE` before building the prefix; added a regression test
  (`test_get_logs_strips_prefix_when_code_file_is_a_full_path`) using a fixture with a real
  full path, and re-verified live against the same real module afterward — lines now match
  the file backend's shape exactly (confirmed byte-for-byte against a real `camera.py`-style
  full-path entry).
- **Verified live, full round trip:** a real `pyobs.modules.Module` instance (scratch
  config/run/log dirs, not touching the real `PYOBS_CONFIG_DIR`) started via
  `services.start_module` with `PYOBS_LOG_BACKEND = "journald"`, confirmed present in the
  journal via plain `journalctl`, then read back correctly through `services.get_logs`
  (matching file-backend line shape) and `services.get_log_stats` (correct per-level counts)
  — then stopped cleanly via `services.stop_module`, confirming process management is
  genuinely unaffected by the log backend, as the Design section's "What doesn't change"
  claimed.
- `README.md` updated: `PYOBS_LOG_BACKEND` added to the Configuration reference block, and
  "How modules are managed" now documents the `--syslog` branch and journald log reads.
- **Not done — the one item left in Open questions**: testing the genuinely group-less
  service account negative case. Doesn't block shipping this, since it's a deploy-time
  permissions question, not a code-correctness one.
- **Done — v1.1, auto-detect `PYOBS_LOG_BACKEND` from `pyobsd`'s own config, not a Work Plan item.** Requested after learning `pyobsd` (`pyobs-core`'s daemon manager, `pyobs-core/pyobs/cli/pyobsd.py` — read directly, this app has no dependency on it) has its own global config file with a `syslog` key it already uses to decide `--syslog` vs. not, when it starts modules itself. `modules/services.py`: `_pyobsd_config()` reads that same file — the exact candidate-path list and "first one found wins" order as `pyobs-core/pyobs/cli/_cli.py`'s `CLI._load_config` (`~/.config/pyobs.yaml`, `/etc/pyobs.yaml`, `/opt/pyobs/storage/pyobs.yaml`) — and returns just its `pyobsd` section, `{}` if no candidate exists or the file is malformed (never raises; this is a convenience auto-detection, not something that should ever break a page load). `_log_backend()` (the single existing choke point every journald-vs-file branch already called through) now checks `settings.PYOBS_LOG_BACKEND` first — if explicitly set (`"file"`/`"journald"`), that always wins, so any deployment that already configured this keeps working completely unchanged — and only falls through to `"journald" if _pyobsd_config().get("syslog") else "file"` when it's unset. `pyobs_web_admin/settings.py`'s default changed from `"file"` to `None` specifically so `_log_backend()` can tell "admin explicitly wants file" apart from "never configured, please auto-detect" — before this change every installation implicitly had an explicit `"file"` value (Django's settings loading always supplies the module-level default), so auto-detection could never have activated for anyone. Tests: `PyobsdAutoDetectTests` in `modules/tests.py` (10 tests — no-candidate-file, reads the `pyobsd` section, missing section, malformed YAML doesn't crash, first-existing-candidate-wins, auto-detects both `journald` and `file`, explicit setting overrides auto-detection in both directions), all patching `services._PYOBSD_CONFIG_CANDIDATES` to a controlled temp path rather than touching the real candidate locations, so results don't depend on whatever happens to exist on the machine running the tests. Verified live via `manage.py shell` against the real settings module (not just the test harness): confirmed a real temp `pyobs.yaml` with `syslog: true`/`false` correctly auto-detects `journald`/`file` when `PYOBS_LOG_BACKEND` is unset, and confirmed an explicit `PYOBS_LOG_BACKEND` setting overrides auto-detection even when the file disagrees. `python manage.py test modules` — 122/122 passing, no regressions (this repo's real `local_settings.py` already has `PYOBS_LOG_BACKEND = "journald"` set explicitly, confirmed to still take priority over auto-detection unchanged).
- **Done — v1.3, resolves the last Open question with a real group-less negative case.**
  `iagvtsrv`'s `pyobs` account (the dedicated, minimal-privilege system user that actually runs
  pyobs modules there — not a repurposed admin/dev account like `husser` in the v1.2 test) ran
  `journalctl SYSLOG_IDENTIFIER=pyobs -n 50 --no-pager` directly and got journald's own hint —
  `"You are currently not seeing messages from other users and the system. Users in groups
  'adm', 'systemd-journal' can see all messages."` — followed by `-- No entries --`, despite
  matching entries existing in the journal. Confirms what the v1.2 test could only infer:
  a genuinely group-less account **is** denied, the same way pyobs-web-admin's own
  `_journalctl_json()` would be if the account running it lacks the grant — and since that
  function never checks `returncode`/`stderr` (see Read layer section), the denial surfaces
  only as silently empty logs in the UI, not an error. Fix applied: `usermod -aG systemd-journal
  pyobs` (see "Which group to grant" below for why `systemd-journal` over `adm`), then a fresh
  login/session for the new group membership to take effect — confirmed this closes the gap.
  **Whichever account actually runs `pyobs-web-admin` itself needs the same grant**, not
  necessarily the `pyobs` account — they're commonly different, and only the process calling
  `journalctl` (the Django app's own process) needs journal read access.
- **Done — v1.2, load older logs on scroll-to-top, not a Work Plan item.** Requested after using the log windows and finding no way to see anything further back than the last `lines` tail without bumping the (capped-at-2000) `lines` param and re-fetching everything. `modules/services.py`: `get_logs`/`get_all_logs` gained an optional `before: datetime | None`, threaded through to `_get_logs_journald`/`_get_all_logs_journald`, which add `--until "<before> UTC"` ahead of the existing `-n <lines>` flag — the same temporal-boundary idiom `_get_log_stats_journald`'s `--since` already established, just the other direction. `journalctl -n <lines> --until <ts>` returns the last `<lines>` entries at or before `<ts>`, exactly "the page of older lines immediately before what's already on screen." The file backend (`tail -n`) has no seek/offset concept to page further back with, so a `before` request there returns `[]` rather than silently re-serving the same tail on every scroll — this makes "load older logs" a journald-only capability for now, not a half-working one on file-backed installs (see Design's new "Pagination: load older logs" section for the full read/API/frontend design). `modules/views.py`: `api_logs`/`api_all_logs` gained a `before` query param (`_parse_before`, same tolerant ISO-8601-with-`Z` parsing `api_all_log_stats`'s `acks` param already uses), forwarded unchanged to a remote hub host's own identical endpoint. `templates/modules/detail.html` (Logs tab) and `all_logs.html` (kept in lockstep, as this app's existing convention already does for these two near-identical log windows) both gained a `scroll` listener on `#log-output` that fetches older lines once scrolled within 40px of the top, deduping the response against already-loaded lines by exact string match (`--until` is inclusive, so the boundary line can reappear) and restoring scroll offset by the exact height delta added, so the line under the viewport doesn't jump. Tests: new cases in `GetLogsJournaldTests`/`GetAllLogsTests` (`before` → `--until`; file backend returns `[]` for a `before` request) and a new `ApiLogsBeforeParamTests` class (`_parse_before` parsing, `api_logs`/`api_all_logs` forwarding) in `modules/tests.py`. `python manage.py test modules` — 176/176 passing, no regressions. **Verified live** with a fake `journalctl` (a small Python script placed ahead of the real binary on `PATH`, since this dev box has no real journald) serving 1000 synthetic timestamped entries for a scratch module: both the per-module Logs tab and the fleet-wide All Logs page correctly paged all the way back to entry 0 across repeated scroll-to-top interactions, with no duplicate lines, correct scroll-position preservation, and "Beginning of available logs" shown exactly once the journal was exhausted, no console errors.

## Motivation

`DEVELOPMENT.md`'s Ideas list carried this as: "systemd/journald logs as an alternative to
file logs. Modules run under systemd instead of `pyobs`'s own file-based logging
(`services.get_logs`, `PYOBS_LOG_DIR`) would have their logs in the journal instead — support
reading from there as an alternative source, not just files." The ask, made concrete in this
doc: one switch that both **starts** pyobs modules logging into the systemd journal instead
of a flat file, and **reads** them back from there for the existing log viewer — not two
independent settings that could drift out of sync with each other.

## Current state

- `modules/services.py`'s `start_module(name)` spawns `pyobs --pid-file ... --log-file ...
  --log-level ... config.yaml` via `subprocess.run` (pyobs double-forks/daemonizes itself);
  `get_logs(name, lines, filter_str)` reads the resulting flat file via `tail -n <lines>` and
  a substring filter; `get_log_stats(name)` binary-searches the same file by byte offset to
  find the start of a 24h window (a workaround that only exists because a flat file has no
  time index), then regexes each line for a `[LEVEL]` tag (`_LOG_LEVEL_RE`) and a leading
  `YYYY-MM-DD HH:MM:SS` timestamp (`_TS_RE`) to bucket counts. This repo has **no
  `pyobs-core` dependency** (per `README.md`) — it only ever invokes `pyobs` as a subprocess
  with CLI flags, never imports it.
- `pyobs-core`'s CLI (checked directly: `pyobs --help`) already has a `--syslog` flag: "send
  log to systemd journal" — this is not something `pyobs-web-admin` needs to add upstream,
  only consume. Reading `pyobs/application.py` (this repo's local `pyobs-core` checkout)
  confirms what it does: when `syslog=True`, it builds a
  `logging_journald.JournaldLogHandler` subclass hardcoded with `identifier="pyobs"` (i.e.
  **every** module's journal entries carry the same `SYSLOG_IDENTIFIER=pyobs` — this is not a
  per-module value) and appends a structured `PYOBS_MODULE=<name>` field per record, where
  `<name>` is `Path(config).stem` — exactly the same `name` this app already uses everywhere
  (`camera`, `telescope`, ...). This handler runs *alongside* the file/stream handlers, not
  instead of them — `--syslog` and `--log-file` are independent flags pyobs accepts
  together, though this doc's design only ever passes one or the other (see Design).
- The journal formatter pyobs uses (`"%(pyobs_module)s %(filename)s:%(lineno)d %(message)s"`)
  deliberately omits the timestamp and `[LEVEL]` tag that the file formatter includes,
  because journald already captures both natively as structured fields (`__REALTIME_TIMESTAMP`,
  `PRIORITY`) — confirmed by reading the source's own comment ("journal omits
  timestamp/priority since those are captured natively by journald") and by observing real
  emitted entries (see Design).
- No existing dependency on `journalctl`/`python-systemd`/`cysystemd` anywhere in this repo.

## Design

### Settings

```python
PYOBS_LOG_BACKEND = None       # None (default): auto-detect from pyobsd's own config;
                                # "file" or "journald": explicit override
```

One fleet-wide switch, grouped with the existing `PYOBS_LOG_DIR`/`PYOBS_LOG_LEVEL` settings —
matches how those are already global, not per-module; this app has no existing mechanism for
per-module settings (every per-module distinction today comes from that module's own YAML
config, and log backend isn't part of the `acl:`/`comm:` surface). **Settled: global-only, no
per-module override** — revisit only if a real fleet needs a migrate-one-module-at-a-time mix,
same reasoning `DEV_ACL_GROUPS.md` uses for deferring its own similar questions.

**v1.1: auto-detected from `pyobsd`'s own config, not a manual setting by default.**
`pyobsd` (`pyobs-core`'s daemon manager, `pyobs-core/pyobs/cli/pyobsd.py`) reads a global
config file (`~/.config/pyobs.yaml`, `/etc/pyobs.yaml`, or `/opt/pyobs/storage/pyobs.yaml`,
first one found wins — `pyobs-core/pyobs/cli/_cli.py`'s `CLI._load_config`) with its own
`pyobsd: syslog: true/false` key, which decides whether *it* starts modules with `--syslog`.
Requiring `PYOBS_LOG_BACKEND` set separately in `local_settings.py` meant it could silently
drift out of sync with what `pyobsd` actually does — reading the same file removes that risk
entirely for anyone who doesn't explicitly override it. `PYOBS_LOG_BACKEND`'s default changed
from `"file"` to `None` so this app can tell "never configured, please auto-detect" apart from
"admin explicitly wants file" — an explicit `"file"`/`"journald"` setting always wins over
auto-detection, so any existing deployment that already set this keeps working unchanged.

**Settled: switching backends is a clean cutover, not a migration.** A module's log history
written under the old backend becomes invisible to `get_logs` once `PYOBS_LOG_BACKEND` flips —
no dual-read, no "check both, merge" reader. Matches this app's existing "no silent fallback
across backends" preference (see `DEV_EJABBERD_INTEGRATION.md`'s HTTP-vs-`ejabberdctl` design,
which picks one path deterministically and never catches failures to fall back). An operator
who wants old file logs preserved keeps the file itself around externally; this app makes no
attempt to reconcile the two.

### `start_module()`: which CLI flags change

When `PYOBS_LOG_BACKEND == "journald"`: pass `--syslog` instead of `--log-file`. Nothing else
changes — `--pid-file`, `--log-level`, and the config argument are identical either way, and
so is every other function in `services.py` (`stop_module`, `get_module_status`,
`restart_module`, `get_module_stats`). Confirmed by reading `pyobs/application.py`:
`--pid-file`'s daemonization is handled entirely separately from the logging-handler setup
this doc touches, so there's no interaction to worry about. `pyobs-web-admin` itself needs no
new dependency — `logging_journald` is a `pyobs-core` dependency, invoked inside the
subprocess this app already spawns, not inside this app's own process.

### Read layer: `get_logs()` / `get_log_stats()`

Journald branch shells out to `journalctl` (present on any systemd host already, no new
dependency) rather than a Python journald-binding library — matches this app's existing
"shell out to a small CLI and parse its output" pattern (`tail`, `ejabberdctl`), and avoids a
native-extension dependency (`python-systemd`) this app has never needed before.

**Query shape, verified live on this box** (real `pyobs.application`'s handler class,
instantiated directly and used to emit five real records at DEBUG through CRITICAL, then
read back):

```
journalctl SYSLOG_IDENTIFIER=pyobs PYOBS_MODULE=<name> -n <lines> -o json --no-pager
```

returned exactly the emitted records, one JSON object per line, oldest-first — same order
`tail -n` already returns. Filtering must be on `PYOBS_MODULE`, not `SYSLOG_IDENTIFIER` alone,
since that field is the same literal string (`"pyobs"`) for every module.

**A real quirk, caught by testing, not by reading the source alone.** `pyobs`'s handler
inherits `logging_journald.JournaldLogHandler.LEVELS`, a dict keyed by Python log-level
*number*:

```python
LEVELS = {logging.CRITICAL: 2, logging.DEBUG: 7, logging.FATAL: 0, logging.ERROR: 3,
          logging.INFO: 6, logging.NOTSET: 16, logging.WARNING: 4}
```

`logging.CRITICAL` and `logging.FATAL` are the same integer (`50`) in Python's `logging`
module — so this dict literal silently collapses to `LEVELS[50] == 0`, since the later key
wins on a duplicate. Confirmed by direct lookup (`LEVELS[logging.CRITICAL]` → `0`) and by
emitting a real `CRITICAL`-level record through the exact handler `pyobs/application.py`
builds: the resulting journal entry had `PRIORITY: "0"` (syslog "emerg"), not `2` ("crit") as
the dict's apparent first entry would suggest. Reading the source top-to-bottom gives the
wrong answer here — the read-side reverse-map has to be built from what pyobs actually emits,
not from the dict's literal order:

| journald `PRIORITY` | pyobs level |
|---|---|
| `0` | `CRITICAL` |
| `3` | `ERROR` |
| `4` | `WARNING` |
| `6` | `INFO` |
| `7` | `DEBUG` |

(`2`, `1`, `5` never occur in practice — pyobs only ever logs at the five standard Python
levels, and `CRITICAL` collapses to `0` as shown above.) **Settled: filed upstream** as
[pyobs/pyobs-core#641](https://github.com/pyobs/pyobs-core/issues/641), independent of this
feature — this doc's reverse-map above matches pyobs's actual behavior regardless of
whether/when that gets fixed.

Also confirmed live: the module name lands in **two** journal fields — `PYOBS_MODULE`
(added explicitly by pyobs's own handler subclass) and `EXTRA_PYOBS_MODULE` (the same value,
swept in automatically by the base `logging_journald` library's "any `LogRecord` attribute it
doesn't recognize becomes `EXTRA_<NAME>`" fallback, since `pyobs_module` isn't in that
library's own field map). Harmless duplication, not a bug — but query/read code should match
on `PYOBS_MODULE` (the name `pyobs-core` itself documents and controls) rather than
`EXTRA_PYOBS_MODULE` (an implementation detail of a third-party library this app doesn't
depend on directly and shouldn't need to know about).

**Reconstructing the existing line shape**, so `_LOG_LEVEL_RE`/`_TS_RE`/`filter_str`/templates
need zero changes downstream:

```python
f"{ts:%Y-%m-%d %H:%M:%S} [{level}] ({module}) {code_file}:{code_line} {message}"
```

— `ts` from `__REALTIME_TIMESTAMP` (microseconds since epoch, present on every entry, confirmed
live), `level` from the `PRIORITY` reverse-map above, `module` from `PYOBS_MODULE`,
`code_file`/`code_line` from `CODE_FILE`/`CODE_LINE` (also confirmed present), and `message`
from `MESSAGE` with its own redundant `"<module> <file>:<line> "` prefix stripped back off
(the journal formatter includes that prefix for raw `journalctl` readability, but this app's
reconstructed line already carries the same information in its own fixed slots, so keeping
both would duplicate it). This keeps the switch entirely contained inside these two
functions — nothing else in `services.py`, `views.py`, or any template needs to know which
backend produced a given line.

`get_log_stats()`'s journald branch replaces the file backend's manual byte-offset binary
search (a workaround that only exists because a flat file has no time index) with
`journalctl SYSLOG_IDENTIFIER=pyobs PYOBS_MODULE=<name> --since "-24h" -o json --no-pager`,
confirmed live to correctly bound the query window; counts come directly from each entry's
`PRIORITY` via the same reverse-map, without round-tripping through reconstructed text and
`_LOG_LEVEL_RE` again.

### Pagination: load older logs (v1.2)

**Backend.** `get_logs`/`get_all_logs` gained an optional `before: datetime | None` alongside
the existing `lines`, threaded through to `_get_logs_journald`/`_get_all_logs_journald`, which
add `--until "<before> UTC"` ahead of the existing `-n <lines>` flag — mirroring
`_get_log_stats_journald`'s existing `--since` usage exactly, just the other temporal
boundary. `journalctl -n <lines> --until <ts>` returns the last `<lines>` entries at or before
that instant, which is exactly "give me the page of older lines immediately before what's
already on screen." The file backend (`tail -n`) has no seek/offset concept to page further
back with, so a `before` request there returns `[]` rather than silently re-serving the same
tail on every scroll — this makes "load older logs" a journald-only capability for now, not a
half-working one on file-backed installs.

**API.** `api_logs`/`api_all_logs` (`views.py`) parse a new `before` query param
(`_parse_before`, ISO-8601 with a `Z` suffix, same tolerant malformed-input-returns-`None`
handling `api_all_log_stats`'s `acks` parsing already uses) and forward it straight through —
including to a remote hub host's own identical endpoint, unchanged.

**Frontend.** Both log windows (`detail.html`'s Logs tab, `all_logs.html`) already rendered
into a single `<pre id="log-output">`; this adds one `scroll` listener on it that calls
`fetchOlderLogs()` once scrolled within 40px of the top. It sends the oldest currently-loaded
line's own parsed timestamp as `before`, dedupes the response against `rawLogLines` by exact
string match (`--until` is inclusive, so the boundary line can come back in the next page),
prepends whatever's left, then restores `scrollTop` by the exact height delta the prepend
added — so the line the user was looking at stays in view instead of the viewport jumping. A
small status line above the `<pre>` reads "Loading older logs…" while in flight and
"Beginning of available logs" once a fetch returns nothing new (a full refresh, e.g. clicking
Refresh or toggling a module checkbox on the All Logs page, resets that flag so a later scroll
tries again — new activity could plausibly extend the journal's retained history further back
in the meantime, though in practice it almost never will).

### What doesn't change

Process management — PID file, start/stop, `get_module_status`, `psutil`-based
CPU/memory/uptime stats — is entirely orthogonal to this switch. `--syslog` only changes
where `Application.__init__`'s logging handlers point; it has no effect on daemonization,
which `pyobs`'s own CLI wrapper handles independently of the logging setup this doc touches.

**Settled: retention/rotation stays out of this app's control either way, same as today.**
The knobs differ — file logs rely on external `logrotate` (the file handler is a
`WatchedFileHandler`, chosen specifically for `logrotate` compatibility per its own code
comment); journald has its own retention (`SystemMaxUse=` etc. in `journald.conf`) — but
`pyobs-web-admin` has never managed either, so switching backends doesn't hand this app a new
responsibility, just a different external mechanism doing the same job it already didn't own.

### Cross-user journal read permission — verified live, with one honest gap left

Real cross-user test on this box, not same-session: a genuinely separate account (`pyobs`,
uid 1001, this box's actual dedicated pyobs system user) emitted a journal record via
`sudo -u pyobs`; a different account (`husser`, uid 1000 — the account that would run
`pyobs-web-admin`) then read it straight back with plain `journalctl
SYSLOG_IDENTIFIER=pyobs PYOBS_MODULE=camera_crossuser_test`, no error, no warning, `exit: 0`.
This is a genuine cross-uid read, unlike the earlier same-session test.

**The surprise: `husser` was not in `systemd-journal` at the time and it worked anyway** —
contradicting this doc's original assumption that group membership would be required. Root
cause, checked afterward: `husser`'s existing groups (`adm`, `dialout`, `cdrom`, `sudo`, `dip`,
`plugdev`, `lpadmin`, `docker`, `ollama`, `sambashare`) include `adm`, which on
Debian/Ubuntu-family systems is granted read access to `/var/log/journal` by a default
`systemd-tmpfiles` ACL rule, not just `systemd-journal` group ownership. Adding `husser` to
`systemd-journal` afterward (`usermod -aG` + `sg systemd-journal -c ...`) also succeeded, as
expected, but was redundant given `adm` already worked.

**v1.3 closes the gap: a truly group-less account, observed for real.** `iagvtsrv`'s `pyobs`
account is exactly the case `husser` couldn't test — a dedicated, minimal-privilege service
account, in neither `adm` nor `systemd-journal`, created specifically to run pyobs modules (not
a repurposed admin/dev account). It ran `journalctl SYSLOG_IDENTIFIER=pyobs -n 50 --no-pager`
directly and was denied — journald's own hint named the two groups, then `-- No entries --`
despite real matching entries existing. `usermod -aG systemd-journal pyobs` plus a fresh
session fixed it. So both halves are now confirmed on real accounts: `adm` is sufficient
(`husser`, v1.2) and the group-less case really is denied, not just theoretically deniable
(`pyobs`, v1.3).

**Which group to grant: prefer `systemd-journal` over `adm`.** Both work — `adm` grants journal
access as a side effect of a Debian/Ubuntu default `systemd-tmpfiles` ACL rule on
`/var/log/journal`, not because it's designed for that. Its actual scope is much broader: read
access to `/var/log/*` generally, plus the general "can read most system logs" role Debian has
handed it historically. `systemd-journal` is the purpose-built group for exactly one thing —
journal read access — nothing more. For a dedicated minimal-privilege account like `pyobs`,
granting only what's actually needed argues for `systemd-journal`; reach for `adm` only if the
account already needs (or already has) broader `/var/log` access for some other reason, in
which case adding `systemd-journal` too would be redundant.

## Open questions

None remaining as of v1.3.

## Work Plan

- [x] Add `PYOBS_LOG_BACKEND` setting (`"file"` default / `"journald"`) to
  `pyobs_web_admin/settings.py`, grouped with the existing `PYOBS_*` settings.
- [x] `start_module()`: branch on `PYOBS_LOG_BACKEND` — pass `--syslog` instead of
  `--log-file` when `"journald"`; no other argument changes.
- [x] `get_logs()`: journald branch via `journalctl SYSLOG_IDENTIFIER=pyobs
  PYOBS_MODULE=<name> -n <lines> -o json --no-pager`, reconstructing lines into the existing
  text shape via the verified priority reverse-map above. Unit tests against **real captured
  `journalctl -o json` output**, not invented JSON shapes — this doc already found one real
  surprise (the `CRITICAL`→`0` collapse) that an invented fixture would have missed, the same
  lesson `DEV_EJABBERD_INTEGRATION.md`'s Work Plan item 3 drew from its own trailing-tab bug. →
  `modules/services.py`, tests in `modules/tests.py`. Caught a second real bug along the way
  (`CODE_FILE` full-path-vs-basename mismatch) that even the real fixtures missed — see
  Progress log.
- [x] `get_log_stats()`: journald branch via `--since "-24h" -o json --no-pager`, counting
  directly from each entry's `PRIORITY` field.
- [x] **v1.3, not in the original plan.** Tested the group-less-account negative case for real
  (`iagvtsrv`'s `pyobs` account, genuinely minimal-privilege, not a repurposed admin/dev
  account) — confirmed denial, confirmed the fix, and documented `systemd-journal` as the
  preferred grant over `adm` — see Progress log and the updated "Cross-user journal read
  permission" section.
- [x] `README.md`: document `PYOBS_LOG_BACKEND` once the above is implemented and verified
  live end-to-end — not before, matching this repo's existing practice of not documenting a
  setting in README before it's actually consumed by code (see `DEV_EJABBERD_INTEGRATION.md`'s
  Progress log, Work Plan item 2, for the same reasoning applied there).
- [x] **v1.1, not in the original plan.** Auto-detect `PYOBS_LOG_BACKEND` from `pyobsd`'s own
  config file instead of requiring it set a second time — see Progress log.
- [x] **v1.2, not in the original plan.** Auto-load older log lines when a log window is
  scrolled to the top (journald-backed modules only; the file backend reports "nothing older
  available") — see Progress log and Design's "Pagination: load older logs" section.
