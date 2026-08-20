# Plan: Show a module's logs under both its config name and its comm name

Status: implemented

Issues: #59

## Problem

The log display looks up a module's logs by its config name (the config file stem). But
pyobs-core stamps `PYOBS_MODULE` two different ways (see `pyobs/application.py`'s stem-mismatch
guard; `module.py`'s `execute()` and `background_task.py`'s `BackgroundTask`):

- ordinary logging uses the config file stem (`Path(config).stem`),
- logging inside `execute()`/`BackgroundTask` uses the module's own comm-derived name — for an
  XMPP comm, the `comm.user` itself.

When a config file's stem differs from its module's comm user, querying only the config name
silently loses every `execute()`/`BackgroundTask` line: the module's log window, its stats, and
its stream in the fleet-wide All Logs view are all incomplete.

## Proposal

When fetching logs for a module, also query by its comm name and merge the results, so modules
with a config name != comm user still get complete log output. The common case (comm user equals
the config stem) must stay a single query, and a module with no `comm:` block (e.g.
HttpFileCache) must be unchanged.

## Implementation — `modules/services.py`

- `_log_identities(name)` — resolves every identity `name`'s log lines may be filed under for
  the current backend: the primary identity (`_journald_module_tag(name)` on journald,
  `_active_name(name)` on file) plus `get_comm_user(name)` when it differs from the primary and
  is a plain module name (otherwise unqueryable via journalctl's `PYOBS_MODULE` match). Returns a
  single-element list in the common/no-comm cases.
- `get_logs()` — queries each identity (one `journalctl` call per identity on journald, each
  identity's log file on the file backend) and merges via the existing `merge_log_lines`, so the
  merged stream stays timestamp-ordered and trimmed to the overall last `lines`.
- `get_log_stats()` — sums per-level counts across identities; the file-backend counting is
  extracted into `_get_log_stats_file(name, since)` so it can run per identity.
- `get_all_logs()` — expands each selected module to all its identities, deduped so two modules
  sharing one comm user (a documented real case) add that identity once. On journald this adds
  extra `PYOBS_MODULE=` OR terms; on file it adds the comm log file to the per-module merge.

Hub mode needs no change: a remote host's own `api_logs`/`api_log_stats`/`api_all_logs` call
these same service functions, so remote log fetches get the fix automatically.

## Deliberately not changed

- `get_module_versions()` stays config-stem-only: the "Loaded pyobs packages:" line is logged
  from `Application._main`, outside `execute()`/`BackgroundTask`, so it is never filed under the
  comm name.
- `get_all_logs(names=None)` (the unrestricted "read everything" journald query) already includes
  comm-name entries, since it applies no `PYOBS_MODULE` restriction at all.

## Tests — `modules/tests.py`

- Journald: `get_logs` queries both identities and merges timestamp-ordered; comm user equal to
  the config stem collapses to one query; `get_log_stats` sums both identities' counts.
- File backend: `get_logs` merges the comm-name log file; no-comm modules read exactly one file.
- `get_all_logs`: each selected module expands to its comm identity; a shared comm user is added
  to the journald OR exactly once.
