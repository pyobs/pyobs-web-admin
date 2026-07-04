# pyobs-web-admin: journald-backed module logging — v0.1 (2026-07-04)

## Status

Design only, nothing implemented yet. Promoted from the one-line `DEVELOPMENT.md` Ideas
bullet after a design discussion; several key facts below were checked against real source
(`pyobs-core`'s installed CLI/`application.py`, the `logging_journald` library) and one was
verified live on this dev box by actually emitting journal records through the exact handler
class `pyobs-core` builds and querying them back with `journalctl` — not just assumed from
reading the code. See Design for what's confirmed vs. still open.

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
config, and log backend isn't part of the `acl:`/`comm:` surface). See Open questions for
whether a per-module override is worth adding later.

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
levels, and `CRITICAL` collapses to `0` as shown above.) This is arguably a `pyobs-core` bug
independent of this feature — see Open questions — but this doc's mapping must match actual
behavior, not the apparently-intended one.

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

## Open questions

- **Cross-user journal read permission — not yet verified.** The live test above ran as the
  same user that both wrote and read the journal entries (a same-session ACL journald grants
  automatically). A real deployment where `pyobs-web-admin` runs as one system account and
  pyobs modules run as another (or as root, or under systemd proper) needs that account in
  the `systemd-journal` group (or an explicit journald ACL) to read other users'/units'
  entries — this needs testing against a genuine cross-user setup before shipping, and
  documenting as a deploy step, mirroring `EJABBERD_INTEGRATION.md`'s `sudo -n ejabberdctl`
  wrapper precedent for a structurally similar "extra local permission needed" gap.
- **No dual-read fallback across the switch, by default leaning.** A module's log history
  written before flipping `PYOBS_LOG_BACKEND` becomes invisible to `get_logs` afterward (the
  old flat file is simply never looked at again). Matches this app's existing "no silent
  fallback across backends" preference (see `EJABBERD_INTEGRATION.md`'s HTTP-vs-`ejabberdctl`
  design: picks one path deterministically, never catches failures and falls back) — but
  confirm this is actually wanted here too, since unlike that case there's no way to
  "re-read" the abandoned file short of a separate one-off import.
- **Per-module override — not designed, no evidence it's needed yet.** `PYOBS_LOG_BACKEND` is
  sketched as global-only. If a real fleet needs a mix (e.g. migrating one module at a time),
  this would need per-module state this app doesn't have a mechanism for today; deferred
  until an actual need shows up, same reasoning `ACL_MATRIX.md`'s Groups section uses for
  deferring its own open questions.
- **The `CRITICAL`/`FATAL` priority collision is arguably a `pyobs-core` bug**, independent of
  this feature — right now, any fleet that already runs `pyobs --syslog` directly (outside
  `pyobs-web-admin`) has its `CRITICAL` log lines silently mismarked as journald "emerg" (0)
  instead of "crit" (2). Worth filing upstream regardless of whether this doc gets built; this
  doc's reverse-map has to match pyobs's actual behavior either way, so filing the upstream
  issue doesn't block or get blocked by this work.
- **Retention/rotation isn't controlled by this app either way**, but the knobs differ: file
  logs rely on external `logrotate` (the file handler is a `WatchedFileHandler`, chosen
  specifically for `logrotate` compatibility per its own code comment); journald has its own
  retention (`SystemMaxUse=` etc. in `journald.conf`), which this app has no visibility into
  or control over. Worth a one-line callout wherever this switch ends up documented for
  operators, so switching backends isn't assumed to carry the same retention behavior over.

## Work Plan

- [ ] Add `PYOBS_LOG_BACKEND` setting (`"file"` default / `"journald"`) to
  `pyobs_web_admin/settings.py`, grouped with the existing `PYOBS_*` settings.
- [ ] `start_module()`: branch on `PYOBS_LOG_BACKEND` — pass `--syslog` instead of
  `--log-file` when `"journald"`; no other argument changes.
- [ ] `get_logs()`: journald branch via `journalctl SYSLOG_IDENTIFIER=pyobs
  PYOBS_MODULE=<name> -n <lines> -o json --no-pager`, reconstructing lines into the existing
  text shape via the verified priority reverse-map above. Unit tests against **real captured
  `journalctl -o json` output**, not invented JSON shapes — this doc already found one real
  surprise (the `CRITICAL`→`0` collapse) that an invented fixture would have missed, the same
  lesson `EJABBERD_INTEGRATION.md`'s Work Plan item 3 drew from its own trailing-tab bug.
- [ ] `get_log_stats()`: journald branch via `--since "-24h" -o json --no-pager`, counting
  directly from each entry's `PRIORITY` field.
- [ ] Verify cross-user journal read permissions for real (not just same-user as tested in
  this doc's live check) and document whatever grant is required as a deploy step.
- [ ] `README.md`: document `PYOBS_LOG_BACKEND` once the above is implemented and verified
  live end-to-end — not before, matching this repo's existing practice of not documenting a
  setting in README before it's actually consumed by code (see `EJABBERD_INTEGRATION.md`'s
  Progress log, Work Plan item 2, for the same reasoning applied there).
