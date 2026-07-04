# pyobs-web-admin: journald-backed module logging — v1.0 (2026-07-04)

## Status

**Implemented and verified live end-to-end** (Work Plan items 1–4; see Progress log). Only
one item is still open: whether a genuinely group-less service account is ever actually
denied journal read access (see Open questions) — this doesn't block using the feature, since
the settled `PYOBS_LOG_BACKEND` default (`"file"`) is unaffected either way.

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
PYOBS_LOG_BACKEND = "file"       # "file" (default) or "journald"
```

One fleet-wide switch, grouped with the existing `PYOBS_LOG_DIR`/`PYOBS_LOG_LEVEL` settings —
matches how those are already global, not per-module; this app has no existing mechanism for
per-module settings (every per-module distinction today comes from that module's own YAML
config, and log backend isn't part of the `acl:`/`comm:` surface). **Settled: global-only, no
per-module override** — revisit only if a real fleet needs a migrate-one-module-at-a-time mix,
same reasoning `ACL_MATRIX.md`'s Groups section uses for deferring its own similar questions.

**Settled: switching backends is a clean cutover, not a migration.** A module's log history
written under the old backend becomes invisible to `get_logs` once `PYOBS_LOG_BACKEND` flips —
no dual-read, no "check both, merge" reader. Matches this app's existing "no silent fallback
across backends" preference (see `EJABBERD_INTEGRATION.md`'s HTTP-vs-`ejabberdctl` design,
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

**What's still not verified: a truly group-less account.** `husser` is this box's original
setup-time admin account — it already had `adm` (and `sudo`) before this test ever started, so
this only shows "`adm` is sufficient," not "some grant is always necessary." A dedicated,
minimal-privilege service account created specifically to run `pyobs-web-admin` (the realistic
production case, not a workstation's admin user) would very likely start in *neither* `adm`
nor `systemd-journal` — that negative case (does `journalctl` actually deny/empty-out for such
an account, and does adding it to either group fix it) was never observed here, so the
deploy-step recommendation (add the account to `adm` or `systemd-journal`) is still the right
defensive guidance, just not proven necessary by this test the way it would be by an actual
denial-then-fix pair — same honesty gap `EJABBERD_INTEGRATION.md`'s own cross-host ACL test
flagged ("tested from the same machine... strong evidence, not absolute proof").

## Open questions

- **Whether a genuinely group-less service account is ever actually denied.** This box's test
  account already had `adm`, which turned out sufficient — so the negative case (a fresh
  account in neither `adm` nor `systemd-journal`) remains unobserved. Testing this precisely
  needs a real minimal-privilege account, not a repurposed admin/dev account like the one
  available on this box.

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
  lesson `EJABBERD_INTEGRATION.md`'s Work Plan item 3 drew from its own trailing-tab bug. →
  `modules/services.py`, tests in `modules/tests.py`. Caught a second real bug along the way
  (`CODE_FILE` full-path-vs-basename mismatch) that even the real fixtures missed — see
  Progress log.
- [x] `get_log_stats()`: journald branch via `--since "-24h" -o json --no-pager`, counting
  directly from each entry's `PRIORITY` field.
- [ ] **Deferred — deploy-time, not code.** Test the group-less-account negative case for
  real (see Open questions) — a genuine minimal-privilege service account, not a repurposed
  admin/dev account — then document whatever grant is actually required (`adm` or
  `systemd-journal`) as a deploy step.
- [x] `README.md`: document `PYOBS_LOG_BACKEND` once the above is implemented and verified
  live end-to-end — not before, matching this repo's existing practice of not documenting a
  setting in README before it's actually consumed by code (see `EJABBERD_INTEGRATION.md`'s
  Progress log, Work Plan item 2, for the same reasoning applied there).
