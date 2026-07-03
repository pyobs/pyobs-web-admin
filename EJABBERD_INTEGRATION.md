# pyobs-web-admin: ejabberd integration — v0.2 (2026-07-03, 17:20)

## Status

Design sketch only — no implementation yet. Captures the conversation that led here, plus
(v0.2) the exact `ejabberdctl` command output formats verified against a real, running
ejabberd instance (including a live pyobs module connection), so work can start from this
document rather than re-deriving the reasoning or guessing at output formats once
implementation begins. See DEVELOPMENT.md for the ACL matrix feature this one is related to
but separate from (both surface "who can talk to what," but this one reads live XMPP server
state rather than static config).

## Motivation

`pyobs-web-admin` usually runs on the same host as the `ejabberd` server pyobs-core's comm
layer connects through (`pyobs.comm.xmpp.XmppComm`, per DEVELOPMENT.md's ACL matrix doc).
`ejabberdctl` is ejabberd's own admin CLI — a local daemon controllable via subprocess, the
same architectural shape this app already uses for `pyobs` itself (README: "no `pyobs-core`
dependency — communicates with pyobs directly via subprocess"). Surfacing some of what
`ejabberdctl` already knows closes two real visibility gaps this tool doesn't currently
cover:

- **Process running ≠ XMPP connected.** A module's process can be alive (this app's existing
  status check passes) while its XMPP session is stuck reconnecting after a network blip —
  invisible today, and exactly the kind of mismatch an admin tool like this should surface.
- **Config vs. reality.** A module's `comm.user` might not even be a registered XMPP account
  (typo, stale config, account never created) — a distinct failure mode from "not connected
  right now."

Write actions (registering/unregistering accounts, changing passwords) are explicitly
**out of scope for this document** — much higher blast radius than a read-only status view
(accidentally locking out a production XMPP account mid-observation), and left for a
separate design pass if wanted later.

## Current state

- No ejabberd integration exists in this repo today.
- A module's own XMPP identity lives in its config under `comm.user` (confirmed against a
  real config: `comm: user: camera`) — the JID's local part, resolved the same way
  `services.get_resolved_acl` already resolves `acl:` (via `pre_process_yaml` +
  `yaml.safe_load`, since `comm:` can equally be pulled in via `{include}` or YAML anchors).
- Confirmed with the user: a caller string in an `acl:` block (e.g. `scheduler`) is always
  exactly an XMPP JID's local part — the same identity space `ejabberdctl`'s user-facing
  commands operate on. This isn't used by anything in this doc's v1 scope, but is what would
  let a later pass cross-reference `acl:` callers against `registered_users` for
  typo/staleness detection (see Open questions).
- The hub-proxying mechanism this design reuses already exists: `modules/proxy.py`'s
  `proxy.call(host, method, path, ...)`, and the pattern of one instance exposing a local
  read-only API endpoint that another instance's hub view queries (`GET /api/acl-matrix/`,
  added for the ACL matrix's hub-mode aggregation) is the direct template here.

## Design

### Settings

```python
EJABBERD_ENABLED = False      # does *some* host in this fleet run ejabberd we should query
EJABBERD_HOST = "localhost"   # which host actually runs it -- "localhost" or a HUB_HOSTS name
EJABBERD_DOMAIN = ""          # the XMPP vhost ejabberd serves
EJABBERDCTL = "ejabberdctl"   # path to the script, like PYOBS_EXEC -- not always on PATH
```

`EJABBERD_ENABLED` gates the feature off entirely for installations without ejabberd
co-located anywhere in the fleet (default `False`, matching the "usually," not "always,"
co-location the feature is premised on). `EJABBERD_HOST` is explicit rather than
auto-discovered or probed, matching how `HUB_HOSTS` itself is already explicit config, not
something this app tries to detect — the user confirmed the common case is one shared
ejabberd server for the whole fleet, so this is a single value, not a per-host flag.
`EJABBERD_DOMAIN` is needed for two things: several `ejabberdctl` commands are per-vhost and
take the domain as an argument (`registered_users <domain>`), and `connected_users`-family
commands return full JIDs (`user@domain/resource`) that need the domain stripped before
comparing against the bare caller/`comm.user` strings used elsewhere in this app.

### Data layer

New module `modules/ejabberd.py` (parallel to `services.py`/`proxy.py`), wrapping
`ejabberdctl` subprocess calls and parsing each command's line-based output. Read-only
commands only, per the Motivation section's scope call. **Verified against a real, running
ejabberd 24.12-4 instance** (see below) rather than assumed from documentation alone —
`sudo -n ejabberdctl ...` works passwordlessly on the dev machine this was checked on; a real
deployment would need `EJABBERDCTL` to already be invokable by whatever user runs
`pyobs-web-admin`, the same assumption `PYOBS_EXEC` already makes for `pyobs` itself.

| Command | Gives | Used for |
|---|---|---|
| `status` | Two lines of text; node up ⟺ exit code `0` | Dashboard: is the XMPP backbone itself healthy |
| `stats registeredusers` / `stats onlineusers` / `stats uptimeseconds` | One bare integer each | Dashboard summary tile — one consistent parsing path instead of three different single-purpose commands (`connected_users_number` returns the same number `stats onlineusers` does) |
| `connected_users_info` | One tab-separated line per session: `jid  connection  ip  port  priority  node  uptime  status  resource  statustext` (10 fields; `statustext` empty in practice) | Cross-reference against modules for the "connected" indicator |
| `registered_users <domain>` | One username per line | Later: typo/staleness detection against `acl:` callers |
| `user_sessions_info <user> <domain>` | Same as `connected_users_info` minus the leading `jid` field (9 fields) | Module page: is *this* module's identity connected, since when, from where |
| `get_last <user> <domain>` | `<ISO-8601 timestamp>\t<status>` — `status` is the literal string `ONLINE` if currently connected, otherwise a **freeform last-disconnect reason** (e.g. `Stream reset by peer`), not a fixed enum | Module page: "last connected 3h ago (stream reset by peer)" for a module that looks stuck |
| `check_account <user> <domain>` | **Exit code only** — `0` = registered, `1` = not (stdout prints `Error: false`/`Error: error` on failure, but that's not what to key parsing off of) | Module page: flag a `comm.user` that isn't a real XMPP account at all |

Confirmed on the live instance: registered accounts on this dev box are literally
`admin, camera, mastermind, observer, scheduler, telescope` — i.e. real pyobs module names
plus `admin`, matching this doc's assumption that ejabberd usernames, `acl:` callers, and
`comm.user` all share one identity space. Starting a real `camera` module and re-querying
showed `connected_users` → `camera@localhost/pyobs` (resource is the fixed string `pyobs`,
not per-instance-random) and `get_last camera localhost` → `...Z	ONLINE` while connected,
confirming the "currently connected" vs. "last seen" distinction above. All three
empty-result cases (`connected_users`, `connected_users_info`, `user_sessions_info` with no
matching session) exit `0` with empty stdout — not an error, just "nothing to report."
Querying an unconfigured vhost (`registered_users wrong-domain`) is the clearest failure
signature short of the binary being entirely absent: exit `1`, stdout
`Error: error\nError: "Unknown virtual host"`.

New `services.get_comm_user(name) -> str | None`, resolving a module's config the same way
`get_resolved_acl` does and pulling out `comm.user` (or `None` if the module has no `comm:`
block, or it's malformed — same defensive shape as the ACL resolution functions).

### Hub-mode delegation

Unlike the ACL matrix (every host contributes its own rows, genuinely aggregated), ejabberd
is typically **one** server for the whole fleet, so this isn't a many-hosts aggregation
problem — it's a "delegate to the one host that has it" problem:

- If `EJABBERD_HOST == "localhost"`: call `ejabberdctl` directly via subprocess.
- Otherwise: `proxy.call()` to that host's own new local endpoint (mirrors
  `GET /api/acl-matrix/`) — which, on *that* instance, has its own `EJABBERD_HOST =
  "localhost"` and handles the request locally. This directly answers "what if ejabberd runs
  on a different hub server": point `EJABBERD_HOST` at that server's `HUB_HOSTS` name, and
  every other host in the fleet transparently proxies through to it.

This also means only one instance in the whole fleet needs `EJABBERD_ENABLED = True` +
correctly pointed `EJABBERD_HOST`/`EJABBERD_DOMAIN`/`EJABBERDCTL` (whichever one actually
runs it); every other instance just needs `EJABBERD_ENABLED = True` and `EJABBERD_HOST` set
to that instance's `HUB_HOSTS` name to see the same data.

### Where it surfaces

Per the user's split — dashboard for the fleet-wide picture, module pages for the
per-module detail — rather than folding this into the ACL matrix (that stays config-only,
static; this is live server state):

- **Dashboard**: a summary tile (connected count / registered count / node status) in the
  same row as the existing Total/Running/Stopped/RAM/CPU tiles, plus a small "XMPP
  connected" indicator per module row, distinct from (not replacing) the existing
  process-status dot — the whole point being that these two signals can disagree.
- **Module detail page**: for that module's own `comm.user` — connected-since/IP/resource if
  live (`user_sessions_info`), last-seen if not (`get_last`), and a registered-or-not check
  (`check_account`) to distinguish "not connected right now" from "this account doesn't even
  exist." Natural home: a new stat block in the existing Overview tab, alongside PID/uptime/
  memory/CPU — this is the same kind of "is this module healthy" information, just sourced
  from ejabberd instead of `psutil`.

## Open questions

- ~~Exact `ejabberdctl` output format~~ — **resolved**, see Data layer above: verified against
  a real running instance rather than assumed from documentation.
- **Silent-absence vs. visible "not configured"** when `EJABBERD_ENABLED` is `False` (or
  `True` but unreachable) — leaning toward silently omitting the UI additions entirely when
  disabled (matching how the sidebar's Hosts section only appears when `HUB_HOSTS` is
  actually configured), and a small non-blocking warning (matching the ACL matrix's
  unreachable-host banner) when enabled but the ejabberd host can't be reached.
- **Refresh cadence.** Measured on the live instance: a single `ejabberdctl` subprocess call
  (`connected_users_info` or `stats onlineusers`) takes **~0.5–0.6s wall-clock** — Erlang VM
  startup/RPC dominates, not the query itself. That's roughly two orders of magnitude slower
  than the dashboard's existing 10s-cadence in-process status polling (`psutil`-based, no
  subprocess spawn). Polling every 10s per open dashboard tab would be a real, constant cost
  for a mostly-static number. Leaning toward: dashboard summary tile refreshes on a slower
  cadence (30–60s, closer to the existing log-stats poll) or piggybacks on the *existing*
  10s status-poll's response rather than firing its own separate request every cycle; the
  per-module page's block stays lazy-loaded once per tab-open, matching Config/Logs/ACL.
- **Comm-user resolution edge cases** — what a module with no `comm:` block at all should
  show (presumably just omit the ejabberd stat block entirely, mirroring how the ACL matrix
  treats a module with no `acl:` block), and whether `comm.user` can itself come from a
  shared fragment/anchor the same way `acl:` can (if so, `get_comm_user` likely wants the
  same resolution approach as `get_resolved_acl`, minus the provenance tracking since there's
  no editing use case here).

## Work Plan

- [ ] Add `EJABBERD_ENABLED` / `EJABBERD_HOST` / `EJABBERD_DOMAIN` / `EJABBERDCTL` settings.
- [ ] `modules/ejabberd.py`: subprocess wrapper + output parsing for the read-only command set above; unit tests against captured real `ejabberdctl` output (see Open questions).
- [ ] `services.get_comm_user(name)`: resolve a module's `comm.user` from its config.
- [ ] Local API endpoint(s) exposing ejabberd data, for both direct browser use and hub-proxying (mirrors `/api/acl-matrix/`).
- [ ] Hub-mode delegation: `EJABBERD_HOST == "localhost"` calls directly, otherwise `proxy.call()` to that host's own endpoint.
- [ ] Dashboard: summary tile + per-module "XMPP connected" indicator.
- [ ] Module detail page: session / last-seen / registered-check block in the Overview tab.
