# Plan: server-side log search (grep the real log/journal)

Status: implemented, live-verified at the HTTP/service layer (real `journalctl`, real flat log
file, real `api_logs` view) and click-tested in a real Chrome browser against a scratch dev
server (`detail.html`'s Logs tab: found a 500-lines-back needle, confirmed debounce/auto-refresh
behavior via the network log, confirmed clean fallback on clearing the filter) — see
`journald-logs.md`'s v1.6 Progress log entry for the full account, including the bug the live
check caught: `journalctl --grep` with zero matches exits 1 with empty stderr, fixed in
`_journalctl_json`'s failure check. `all_logs.html` was not independently click-tested (shares
the same JS pattern already exercised on `detail.html`).

Issues: #95

## Problem

The log windows (`templates/modules/detail.html`'s Logs tab, `templates/modules/all_logs.html`)
filter entirely client-side, against only the newest ~300 lines already downloaded
(`applyFilters()`'s `line.toLowerCase().includes(text)`, `detail.html:781`). A search phrase can
never find anything older than that tail, and there's no way to search back through the log at
all. The backend already accepts a `filter` param (`api_logs`, `views.py:593`; `api_all_logs`,
`views.py:632`) and applies it as a Python substring test in `services.get_logs`/`get_all_logs`
(`services.py:1406-1408`, `1602-1604`) — but only *after* fetching the last `lines` (default 300,
capped 2000). No frontend code ever sets it.

**Extra bug found while reading the code, not in the original issue text:** `api_logs`'s
host-proxy branch (`views.py:582-590`, `if host:`) builds its `params` dict from `lines`/`before`/
`since`/`until` only — `filter` is read *after* that branch and never added to `params`, so a
search against a module viewed through a remote hub host is silently dropped today (masked by the
client-side filter re-running on whatever unfiltered page comes back). `api_all_logs`'s per-host
proxy call already does this correctly (`views.py:672-673`, `if filter_str: params["filter"] =
filter_str`). This needs fixing as part of this change — once client-side text filtering is
removed, a dropped `filter` param means search silently does nothing for proxied modules instead
of just being redundant with the client filter.

## Design

### Decision: fully server-side, no client-side text filter

Once the server does the matching, the client renders what comes back — no client-side
`text`/`includes()` pass on top. Keeping one would either be dead code (server results already
match) or a trap: a "just in case" client re-filter over a *page* of server results reproduces the
exact bug this issue is about (silently missing anything the current page doesn't happen to
contain). `applyFilters()`'s other filters (min level, unacked-only, client-side time bounds) are
unrelated to full-text search and stay as they are.

Cost: a network round-trip per search instead of instant local filtering. Mitigated by debouncing
the filter input (already called out in the issue's notes) rather than fetching on every
keystroke.

### File backend: streamed Python scan, not a `grep` subprocess

The issue offers "a real `grep`/streamed scan" as the file-backend option. Going with a streamed
Python scan (open the file, iterate lines, `filter_str.lower() in line.lower()`) rather than
shelling out to `grep`:

- Same case-insensitive substring semantics as today's `filter_str.lower() in line.lower()`
  (`services.py:1407`) — zero behavior change for what counts as a match, just *where* the scan
  starts and how far back it goes.
- No new subprocess/dependency, no need to reason about escaping the phrase for grep's regex
  dialect, easier to unit test than mocking a `grep` subprocess.

New helper, alongside the existing `_get_logs_file`:

```python
def _search_logs_file(name: str, lines: int, filter_str: str, since: datetime | None, until: datetime | None, before: datetime | None) -> list[str]:
    """Streamed substring search over the whole file (or from `since`'s byte offset if given),
    matching filter_str.lower() in line.lower() -- same semantics as today's post-hoc Python
    filter, just applied while scanning instead of after truncating to a tail. Returns at most
    the last `lines` matches (most recent first is not required; caller/merge sorts by
    timestamp)."""
    log_file = _log_dir() / f"{_active_name(name)}.log"
    if not log_file.exists():
        return []
    since_naive = _naive_utc(since) if since is not None else None
    upper = _until_bound(before, until)
    upper_naive = _naive_utc(upper) if upper is not None else None

    with open(log_file, "rb") as f:
        start = 0
        if since_naive is not None:
            f.seek(0, 2)
            file_size = f.tell()
            if file_size == 0:
                return []
            start = _file_offset_of_last_line_before(f, file_size, since_naive)
        f.seek(start)
        if start > 0:
            f.readline()  # skip the (possibly partial) line at the seek point
        buf: deque[str] = deque(maxlen=lines)
        for raw in f:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            t = _file_line_ts(line)
            if since_naive is not None and t is not None and t < since_naive:
                continue
            if upper_naive is not None and t is not None and t > upper_naive:
                break
            if filter_str.lower() in line.lower():
                buf.append(line)
        return list(buf)
```

This reuses `_file_offset_of_last_line_before`/`_naive_utc`/`_until_bound`/`_file_line_ts`
unchanged. `lines` now caps *matches*, not lines scanned, per the issue's point 2 — matching
`_get_logs_file`'s existing `before`-branch `deque(maxlen=lines)` pattern (`services.py:1513`).

**Deliberately out of scope: plain (non-search) pagination without `since` stays as today
(`[]`).** The issue's note that "a server-side grep that reads from byte 0 would also give the
file backend genuine full-history search, which it has none of today" is about *search*
specifically — extending that to unfiltered scroll-back would mean a full-file scan on every
scroll-to-top with no phrase, which the current design deliberately avoids (the doc's own "common
case stays a plain `tail -n`" reasoning, `services.py:1471-1479`). This is called out explicitly
below as a `journald-logs.md` documentation update, not a behavior change.

### Journald backend: add `--grep`, not a new code path

Verified against `man journalctl` on this box (2026-09-14): `-g/--grep=PATTERN` filters entries
whose `MESSAGE=` field matches a PCRE regex; matching is case-insensitive automatically only if
the pattern is all-lowercase (smart-case), overridable with `--case-sensitive[=BOOLEAN]`; **"When
used with `--lines=` (not prefixed with `+`), `--reverse` is implied."** That last point is the
one that matters here: `journalctl -n <lines> --grep=<pattern> --since ... --until ...` already
scans backward from the end of the (bounded) journal and stops once `<lines>` matches are found —
i.e. adding `--grep` to the existing `_get_logs_journald`/`_get_all_logs_journald` invocation
(`services.py:1343-1352`, `1525-1544`) gets "search all the way back, capped at N matches" for
free. No restructuring of the journald read path is needed, unlike the file backend.

Changes to `_get_logs_journald`/`_get_all_logs_journald`:

```python
if filter_str:
    args += ["--grep", re.escape(filter_str), "--case-sensitive=false"]
```

`re.escape` turns the literal phrase into a literal PCRE pattern (matches today's plain-substring
semantics; Python's `re` escaping is PCRE-compatible for the metacharacters that matter here) —
without it, a phrase containing `.`, `(`, `*`, etc. would be interpreted as regex syntax, a
behavior change from today's plain substring test.

**Real, worth-flagging narrowing: `--grep` only sees the raw `MESSAGE=` field, not the
reconstructed display line.** `_journal_entry_to_line` (`services.py:1304-1340`) builds
`f"{ts} [{level}] ({module}) {code_file}:{code_line} {message}"` — the synthetic timestamp and
`[LEVEL]` bracket are added during Python reconstruction and are *not* part of the raw `MESSAGE`
journalctl greps against. Concretely: searching for `ERROR` as a phrase would stop matching lines
by level once client-side filtering goes away, and searching for a raw timestamp string would stop
working. In practice this is a small loss — module name, `file:line`, and the actual log text are
still searchable, because pyobs's own journald formatter bakes `"<module> <file>:<line> "` into
`MESSAGE` itself (`services.py:1312-1316` comment), and there's already a dedicated "Min level"
dropdown (`log-level-min`) for level filtering, so losing level-substring-search from the free-text
box isn't a real capability loss. Flagging this as a deliberate, documented tradeoff rather than a
silent regression — happy to reconsider if it turns out to matter in practice.

### Error handling: distinguish "no matches" from "search failed"

`_journalctl_json` (`services.py:1293-1301`) runs `journalctl` and silently drops anything that
isn't valid JSON, never checking `result.returncode`/`stderr` — a denied or malformed `--grep`
(e.g. a genuinely pathological regex, though `re.escape` should prevent that) surfaces today as a
quiet empty list, indistinguishable from a real "no matches." Add:

```python
def _journalctl_json(args: list[str], check: bool = False) -> list[dict]:
    result = subprocess.run(["journalctl", *args, "-o", "json", "--no-pager"], capture_output=True, text=True)
    if check and result.returncode != 0 and result.stderr.strip():
        raise LogSearchError(result.stderr.strip())
    ...
```

**Correction made during implementation, caught only by live-testing against the real
binary, not by mocked unit tests:** `journalctl --grep <pattern>` with *zero matches* exits 1
with *empty* stdout and stderr — checking `returncode != 0` alone (this section's original
draft) would raise `LogSearchError` on every ordinary non-matching search, which is the single
most common case a search returns. A genuine failure (bad argument, malformed `--since`, etc.)
also exits non-zero but always writes something to stderr, so the check is `returncode != 0
and stderr` together, not returncode alone. `check` defaults `False` so the two unrelated
existing callers (`_get_module_versions_journald`, `_get_log_stats_journald`) keep their old
tolerant behavior unchanged.

`LogSearchError` (new, `services.py`) propagates up through `get_logs`/`get_all_logs` to
`api_logs`/`api_all_logs`, which catch it and return e.g. `JsonResponse({"error": str(e)},
status=502)` instead of `{"lines": [...]}`. Frontend `fetchLogs`/`fetchOlderLogs` (both templates)
check for `data.error` and show a distinct "Search failed: …" status via `setOlderStatus`/a new
inline message, instead of rendering `(no matching log lines)`. This closes the issue's own
"'No matches' should be distinguishable from 'the search failed'" note.

### Fleet-wide `lines` semantics: no change needed, document the existing behavior

`merge_log_lines` (`services.py:1547-1563`) already merges each source's own `lines`-capped result
and trims the *merged* list to `lines` total — this is already "lines is a global cap across
hosts/modules," not a per-host quota, and that's the right semantics for search too (otherwise a
5-host fleet search could return up to `5 × lines` candidates before trimming). No code change;
this resolves the issue's "decide" bullet by confirming the existing merge-then-trim behavior
already does the right thing once each per-host/per-module fetch is itself a real search instead
of an unfiltered tail.

### API layer (`views.py`)

- `api_logs`: move `filter_str = request.GET.get("filter", "")` above the `if host:` branch;
  add `if filter_str: params["filter"] = filter_str` to the proxied `params` dict (the bug found
  above). Wrap the `services.get_logs(...)` call to catch `LogSearchError` → `502` JSON error.
- `api_all_logs`: already forwards `filter` to remote hosts correctly; wrap the local
  `services.get_all_logs(...)` call the same way for `LogSearchError`. A remote host's own 502
  error body should propagate through `unreachable_hosts` or a dedicated error slot rather than
  being swallowed by the existing `except Exception` around `proxy.call` — needs a small addition
  there so a search failure on one host doesn't look identical to that host being offline.

### `services.get_logs`/`get_all_logs`: route to search vs. tail

```python
def get_logs(name, lines=300, filter_str="", before=None, since=None, until=None):
    validate_name(name)
    identities = _log_identities(name)
    if _log_backend() == "journald":
        line_lists = [_get_logs_journald(i, lines, before, since, until, filter_str) for i in identities]
    elif filter_str:
        line_lists = [_search_logs_file(i, lines, filter_str, since, before, until) for i in identities]
    else:
        line_lists = [_get_logs_file(i, lines, since, before, until) for i in identities]
    return line_lists[0] if len(line_lists) == 1 else merge_log_lines(line_lists, lines)
```

(`_get_logs_journald` takes `filter_str` directly rather than branching outside it, since the
journald path is a flag addition to one function, not a separate code path.) The trailing
`if filter_str: log_lines = [... filter_str.lower() in line.lower() ...]` post-filter
(`services.py:1406-1408`, `1602-1604`) is removed — matching now happens inside the backend-
specific read, not as a second pass on top of it. Mirror the same shape in `get_all_logs`/
`_get_all_logs_file`/`_get_all_logs_journald`.

### Frontend (`detail.html`, `all_logs.html`, kept in lockstep per this repo's existing "logs JS is
duplicated in lockstep" convention)

- `applyFilters()`: drop the `text`/`includes()` branch entirely; free-text matching is no longer
  a client concern.
- `fetchLogs()`/`fetchOlderLogs()`: add `filter` to the query params when the filter box is
  non-empty (mirrors how `since`/`until` are already conditionally added).
- Debounce the filter `input` listener (currently just persists to `localStorage` and calls
  `renderLogs()`, `detail.html:924-927`) — e.g. 300ms `setTimeout`/`clearTimeout` before calling
  `fetchLogs()`.
- On phrase change: clear `rawLogLines`, reset `noMoreOlderLogs`/`setOlderStatus('')` (the "Beginning
  of available logs" state), and re-fetch — same pattern `timeStartEl`'s `change` listener already
  uses (`detail.html:936-943`) for the same reason (history in memory no longer matches the new
  query).
- Surface `data.error` (see error-handling section) as a distinct status message instead of
  falling through to "(no matching log lines)".

## Work Plan

1. `services.py`: add `LogSearchError`, `_search_logs_file`, `_journalctl_json` returncode check,
   `--grep`/`--case-sensitive=false` in `_get_logs_journald`/`_get_all_logs_journald`, route
   `get_logs`/`get_all_logs` to search vs. tail, drop the post-hoc substring filter.
2. `views.py`: fix the `api_logs` proxied-`filter` bug; catch `LogSearchError` in both `api_logs`
   and `api_all_logs`; handle a remote host's search-failure response in `api_all_logs`'s
   per-host loop.
3. `detail.html` + `all_logs.html`: send `filter` in `fetchLogs`/`fetchOlderLogs`, debounce the
   filter input, drop client-side text filtering from `applyFilters`, reset history on phrase
   change, surface search-failure errors.
4. `specs/design/journald-logs.md`: new version entry documenting server-side search (both
   backends), the MESSAGE-only journald narrowing, and the deliberate file-backend "search scans
   from byte 0 (or `since`), plain browsing still doesn't page back without `since`" boundary.
5. Tests (`modules/tests.py`) — see Testing below.

## Testing

Follow the existing patterns in `modules/tests.py` (`LogBackendJournaldTests` class,
`@mock.patch("modules.services.subprocess.run")`):

- `_search_logs_file`: temp log file fixtures (existing file-backend tests already write temp
  files, e.g. `test_file_backend_merges_comm_user_log_file`) — match found beyond the last `lines`
  lines of the file; no match; `since` narrows the scan start (verify it doesn't scan bytes before
  the computed offset — could assert via a spy/large file rather than just correctness); `lines`
  caps the number of *matches* returned, not lines scanned; case-insensitivity.
- Journald: assert `--grep`/`--case-sensitive=false` are passed with `re.escape`d input (mirrors
  `test_get_logs_before_adds_until_arg_ahead_of_lines_flag`-style arg-shape assertions); a
  `returncode != 0` from the mocked `subprocess.run` raises `LogSearchError`.
- `api_logs`: proxied request includes `filter` in the forwarded params (regression test for the
  bug found above); a `LogSearchError` from `services.get_logs` yields a non-`lines` JSON error
  response.
- `merge_log_lines`: no new test needed (unchanged), but keep an existing test around it as
  evidence the "global cap, not per-host quota" semantics already hold.

No frontend automated tests exist for this JS today (per the fullscreen-button plan's precedent);
verify manually: search a phrase only present outside the last 300 lines, confirm results without
scrolling first; clear/retype a phrase and confirm the "Beginning of available logs" state resets;
disconnect from a remote hub host mid-search (or otherwise force a `LogSearchError`) and confirm
the UI shows a failure state, not silently empty results.

## Out of scope

- Regex search (only literal substring, matching today's semantics — `re.escape` deliberately
  prevents user-supplied regex).
- Extending unfiltered (no search phrase) file-backend pagination to work without `since` —
  documented as a known boundary, not fixed here (see Design above).
- A keyboard shortcut or other UI nicety beyond the debounce/reset already required for correct
  behavior.

## Related

- `specs/design/journald-logs.md` — the living design doc this plan's work folds into once
  shipped (v1.6 entry, per that doc's own versioning convention).
- #59 (logs for both config name and comm name) — `_log_identities`/merge behavior this plan's
  search functions must also respect (both `_search_logs_file` and the journald `--grep` path are
  invoked once per identity, same as today's tail read).
