# pyobs-web-admin: ejabberd integration — v0.6 (2026-07-03, 20:30)

## Status

Design settled (v0.2–v0.5, see version history in git blame if needed), implementation
starting now — see **Progress log** below for exactly what's done and what's next, kept
current the same way ACL_MATRIX.md's is. `ejabberdctl` is kept as a documented fallback to
`mod_http_api`, not deleted from the plan. IP-only (`acl: loopback`) is the settled v1
security model — see "Credential layer investigation" in Security model for why. See
ACL_MATRIX.md for the ACL matrix feature this one is related to but separate from (both
surface "who can talk to what," but this one reads live XMPP server state rather than
static config).

## Progress log

Work Plan items are implemented top-to-bottom; check the item list at the bottom for the
authoritative state, this log just narrates it.

- **Done — Work Plan item 1.** `EJABBERD_ENABLED` / `EJABBERD_HOST` / `EJABBERD_DOMAIN` /
  `EJABBERD_API_URL` / `EJABBERDCTL` added to `pyobs_web_admin/settings.py`, defaults matching
  this doc's Settings section exactly (`EJABBERD_ENABLED = False`, `EJABBERD_API_URL =
  "http://127.0.0.1:5281/api"`, etc.), grouped with the existing `PYOBS_*` settings and
  overridable the same way via `local_settings.py`. `python manage.py check` and
  `python manage.py test modules` both pass unchanged (settings-only change, no behavior
  yet). Not yet consumed by any code — the next items (`modules/ejabberd.py`, `get_comm_user`)
  are what actually read these.

## Motivation

`pyobs-web-admin` usually runs on the same host as the `ejabberd` server pyobs-core's comm
layer connects through (`pyobs.comm.xmpp.XmppComm`, per ACL_MATRIX.md's ACL matrix doc).
ejabberd exposes the same underlying admin commands through several independent interfaces
(`ejabberdctl`, the CLI; `mod_http_api`, HTTP+JSON; others) — either way, this is a local
daemon this app can already query directly, the same architectural shape this app already
uses for `pyobs` itself (README: "no `pyobs-core` dependency — communicates with pyobs
directly via subprocess"). Surfacing some of what ejabberd already knows closes two real
visibility gaps this tool doesn't currently cover:

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
EJABBERD_ENABLED = False                       # does *some* host in this fleet run ejabberd we should query
EJABBERD_HOST = "localhost"                     # which host actually runs it -- "localhost" or a HUB_HOSTS name
EJABBERD_DOMAIN = ""                            # the XMPP vhost ejabberd serves
EJABBERD_API_URL = "http://127.0.0.1:5281/api"  # mod_http_api base URL -- primary mechanism, see Data layer
EJABBERDCTL = "ejabberdctl"                     # path to the script, like PYOBS_EXEC -- fallback only
```

`EJABBERD_ENABLED` gates the feature off entirely for installations without ejabberd
co-located anywhere in the fleet (default `False`, matching the "usually," not "always,"
co-location the feature is premised on). `EJABBERD_HOST` is explicit rather than
auto-discovered or probed, matching how `HUB_HOSTS` itself is already explicit config, not
something this app tries to detect — the user confirmed the common case is one shared
ejabberd server for the whole fleet, so this is a single value, not a per-host flag.
`EJABBERD_DOMAIN` is needed for two things: several commands are per-vhost and take the
domain as an argument (`registered_users`), and `connected_users`-family results return full
JIDs (`user@domain/resource`) that need the domain stripped before comparing against the
bare caller/`comm.user` strings used elsewhere in this app. `EJABBERD_API_URL` defaults to
loopback, matching the ejabberd-side config below, which restricts this API to loopback
callers specifically — pointing it anywhere else requires loosening that ACL too, a
deliberate two-key lock rather than a single default that's easy to widen by accident.

### Data layer

**Primary mechanism: `mod_http_api` over HTTP, not `ejabberdctl` subprocess calls.** v0.2
planned a subprocess wrapper around `ejabberdctl`; both are now verified against the same
live ejabberd 24.12-4 instance, including with a real connected pyobs module, and the
difference is decisive:

| | `ejabberdctl` (subprocess) | `mod_http_api` (HTTP) |
|---|---|---|
| Latency per call | ~0.5–0.6s (Erlang VM boot + distribution handshake per invocation — confirmed by reading `/usr/sbin/ejabberdctl` itself, not assumed) | ~0.01s (hits the already-running node directly) |
| Response format | Line-based text, tab-separated fields, inconsistent per command (see v0.2's table) | Clean JSON per command, one shape |
| Extra ejabberd-side config | None | `mod_http_api` enabled + `api_permissions` grant (see below) |
| Dependency | Subprocess spawn | `requests` (already a dependency, used for hub proxying) |

Given the user's "we always have full control over the ejabberd server" — the one
precondition that made `ejabberdctl`-via-subprocess attractive (zero extra config) doesn't
actually hold as a constraint, so there's no reason to accept 50–60x the latency. New module
`modules/ejabberd.py` (parallel to `services.py`/`proxy.py`) wraps `EJABBERD_API_URL` calls
via `requests.post(f"{EJABBERD_API_URL}/{command}", json=args)`; each command returns JSON
directly, no custom text parsing needed. `ejabberdctl` is kept as a **documented fallback**
(e.g. if `EJABBERD_API_URL` is unset or unreachable) for hosts that haven't done the
ejabberd-side setup yet, not removed from the plan — see Work Plan.

| Command | Response (JSON) | Used for |
|---|---|---|
| `status` | A string like `"The node ejabberd@localhost is started. Status: started  ejabberd 24.12-4 is running in that node"` | Dashboard: is the XMPP backbone itself healthy |
| `stats` (`{"name": "registeredusers"\|"onlineusers"\|"uptimeseconds"}`) | A bare integer | Dashboard summary tile |
| `connected_users_info` (`{}`) | `[{"jid", "connection", "ip", "port", "priority", "node", "uptime", "status", "resource", "statustext"}, ...]` | Cross-reference against modules for the "connected" indicator |
| `registered_users` (`{"host": ...}`) | `["admin", "camera", ...]` | Later: typo/staleness detection against `acl:` callers |
| `user_sessions_info` (`{"user": ..., "host": ...}`) | Same shape as one `connected_users_info` entry, minus `jid` | Module page: is *this* module's identity connected, since when, from where |
| `get_last` (`{"user": ..., "host": ...}`) | `{"timestamp": "...", "status": "ONLINE"}` while connected, otherwise a **freeform last-disconnect reason** in `status` (e.g. `"Stream reset by peer"`) — not a fixed enum | Module page: "last connected 3h ago (stream reset by peer)" for a module that looks stuck |
| `check_account` (`{"user": ..., "host": ...}`) | HTTP `200` either way, body is a bare integer: `0` = registered, `1` = not (confirmed against both a real and a nonexistent account) | Module page: flag a `comm.user` that isn't a real XMPP account at all |

Confirmed live, end to end: `registered_users` → exactly
`["admin","camera","mastermind","observer","scheduler","telescope"]` (matching this doc's
assumption that ejabberd usernames, `acl:` callers, and `comm.user` all share one identity
space); starting a real `camera` module and re-querying `connected_users_info` returned
`{"jid":"camera@localhost/pyobs","connection":"c2s_tls","ip":"::1",...,"resource":"pyobs",...}`
— resource is the fixed string `pyobs`, not per-instance-random.

**Access control, verified by testing the denial path, not just the happy path:** calling an
admin-tagged command (`registered_users`, `stats`) with no `api_permissions` grant configured
returned `403 Forbidden` with `{"code":32,"message":"AccessRules: Account does not have the
right to perform the operation.", ...}` — confirming `mod_http_api`'s default is deny, not
allow, for anything beyond a handful of commands ejabberd itself tags as harmless (`status`
worked with zero configuration). After adding the `api_permissions` grant below, the same
calls returned `200` with correct data. This means the ejabberd-side config isn't just
plausible on paper — it was confirmed to actually gate access, not merely assumed to.

#### ejabberd-side configuration (verified working)

```yaml
listen:
  -
    port: 5281
    ip: "::"                      # as configured on the box this was tested on -- reachable
                                   # on all interfaces, so the api_permissions ACL below is
                                   # the *real* security boundary, not a loopback bind
    module: ejabberd_http
    request_handlers:
      /ws: ejabberd_http_ws        # pre-existing, unrelated to this feature
      /api: mod_http_api           # <- added

modules:
  mod_http_api: {}

api_permissions:
  "console commands":
    from: [ejabberd_ctl]
    who: all
    what: "*"
  "pyobs-web-admin readonly":
    from: [mod_http_api]
    who:
      access:
        allow:
          - acl: loopback
    what:
      - "status"
      - "stats"
      - "connected_users_info"
      - "registered_users"
      - "user_sessions_info"
      - "get_last"
      - "check_account"
```

Only add `/api: mod_http_api` to an **existing** `ejabberd_http` listener's
`request_handlers` if one's already there for something else (e.g. `/ws` for
BOSH/WebSocket) — ejabberd allows one listener per port, so a second `listen:` entry on the
same port would conflict, not layer on top. The `what:` list is a deliberate whitelist, not
`"*"` — `mod_http_api` can also expose `register`/`unregister`/`change_password`/etc., and
none of those should be reachable even from loopback, per this doc's write-actions-out-of-
scope decision. Applying this required no ejabberd restart on the instance it was tested on
(a config reload was sufficient) — worth reconfirming per ejabberd version, since listener
changes sometimes do require a restart.

#### Security model: IP-based, not credential-based — verified, not assumed

**No password, API key, or token is involved at all.** The only gate is `acl: loopback` in
`api_permissions`, which is purely a source-IP check applied by `mod_http_api` per request —
there is no username/password or bearer token layer configured (`mod_http_api` supports
adding HTTP Basic Auth against an ejabberd account, or an OAuth bearer token via
`oauth_issue_token`, as optional additional layers; neither is configured here, by choice, to
keep this scoped to what's actually needed).

This was tested against a real non-loopback request, not just asserted from ejabberd's docs,
after noticing the listener's `ip: "::"` binds the TCP socket on *every* interface — that's a
different, separate layer from the ACL, and worth not conflating:

- The **listener binding** (`ip: "::"`) controls what can *connect* — with `"::"`, literally
  anyone who can route to port 5281 can open a connection and send a request. Nothing at the
  socket level blocks that.
- The **`api_permissions` ACL** is a *request-level* check `mod_http_api` applies after the
  connection is accepted — it inspects that request's source IP and only lets the command
  through if it matches `loopback`.

Verified by curling `192.168.178.246:5281` (the box's own real LAN-facing IP, not
`127.0.0.1`) instead of loopback: **every command tested returned `403 Forbidden`** —
including `registered_users`/`stats` (in the explicit whitelist) *and* `status` (which,
before the custom `api_permissions` block existed, had worked with zero configuration at
all — confirming the custom block now governs `status` too, not just the commands explicitly
listed in `what:`). The identical request against `127.0.0.1` succeeded. This is real
evidence the ACL inspects the actual connecting peer's address rather than something
trivially bypassable from userspace — **with one honest caveat: this was tested from the same
machine, addressed to its own LAN-facing IP, not from a genuinely separate remote host**,
since none was available to test from. Strong evidence, not an absolute proof of the
cross-host case.

**What this security model does and doesn't protect against**, stated plainly rather than
left implicit:

- **Does protect against**: any request arriving from outside this machine, over the
  network — confirmed above.
- **Does not protect against**: any *other* local process or user account on the same
  machine. The ACL is IP-based, not identity-based — it can't distinguish
  "`pyobs-web-admin` specifically" from "anything else running on this box that can reach
  `127.0.0.1:5281`." Whatever it can read (registered usernames, live session IPs/ports,
  connection metadata for every connected entity) is available to any local process, not
  scoped to this app. On the dedicated, single-purpose observatory-control machine this
  feature is premised on, that's a small and likely acceptable residual risk — but it's a
  conscious tradeoff, not an oversight, and worth documenting as one rather than glossing
  over it because the loopback case works.
- **Decided: not adding a credential layer, after actually trying.** See "Credential layer
  investigation" below — this residual risk (any other local process on the same machine)
  is accepted for v1, not because it wasn't considered, but because both credential options
  were attempted for real on the live instance and neither panned out within reasonable
  effort, while IP-only is already fully verified and covers the actual threat model this
  feature cares about (network-remote access).

#### Credential layer investigation — tried, not adopted

Two options were tried live, not just discussed on paper:

- **OAuth bearer token** (`oauth_issue_token`): failed outright with an ejabberd-internal
  error — `undefined function oauth2:authorize_password/3` — a missing dependency/module in
  this build, not a config mistake. Dead end without digging into ejabberd's own OAuth setup
  further, which wasn't judged worth it for this feature.
- **HTTP Basic Auth** against a dedicated new account (`webadmin@localhost`, registered
  specifically for this test and unregistered again afterward — no dangling account left
  behind): the credential itself was confirmed valid (`ejabberdctl check_password` succeeded)
  and the `Authorization` header was confirmed correctly formed (base64-decoded and checked
  by hand), yet every request still came back `401` with `{"code":10,"message":"You are not
  authorized to call this command."}` — a different, less specific error than the plain
  `403 AccessRules` seen with no grant at all. Checked, in order: whether `acl:` and `user:`
  compose inside one `access.allow:` entry the way ejabberd's own docs show for `ip:` +
  `user:` (switched to the documented `ip: 127.0.0.1/8` form exactly — no change); whether a
  vhost mismatch was the cause (`mod_http_api` resolves its vhost from the HTTP `Host` header,
  not the JSON body's `"host"` field — a real, separate gotcha discovered along the way: a
  request to `127.0.0.1:5281` logged `Using module mod_http_api for host 127.0.0.1, but it
  isn't configured` since only `localhost` is a registered vhost; switching to
  `http://localhost:5281/...` fixed *that* warning but not the 401). ejabberd's own log
  (`[info]`-level `mod_http_api:log/3` line) recorded the call happened but gave no further
  detail on why authorization failed. Likely explanation, unconfirmed: the `user:` condition
  inside `api_permissions`'s `access.allow` may be designed around OAuth-authenticated
  identity (which is broken on this build anyway, see above) rather than HTTP Basic Auth,
  despite ejabberd's own documentation example reading as if it were auth-method-agnostic.

Stopping here rather than continuing to iterate against a live server for a security layer
whose absence is an accepted, documented tradeoff — not a blocker. Revisit if: this ejabberd
instance's OAuth support gets fixed/enabled properly, or a future need changes the threat
model (e.g. this machine stops being single-purpose).

New `services.get_comm_user(name) -> str | None`, resolving a module's config the same way
`get_resolved_acl` does and pulling out `comm.user` (or `None` if the module has no `comm:`
block, or it's malformed — same defensive shape as the ACL resolution functions).

### Hub-mode delegation

Unlike the ACL matrix (every host contributes its own rows, genuinely aggregated), ejabberd
is typically **one** server for the whole fleet, so this isn't a many-hosts aggregation
problem — it's a "delegate to the one host that has it" problem:

- If `EJABBERD_HOST == "localhost"`: call `EJABBERD_API_URL` directly (loopback, per the
  ejabberd-side config above — this instance and ejabberd are on the same box, so the
  `acl: loopback` grant covers it).
- Otherwise: `proxy.call()` to that host's own new local endpoint (mirrors
  `GET /api/acl-matrix/`) — which, on *that* instance, has its own `EJABBERD_HOST =
  "localhost"` and hits its own loopback `EJABBERD_API_URL` locally. This directly answers
  "what if ejabberd runs on a different hub server": point `EJABBERD_HOST` at that server's
  `HUB_HOSTS` name, and every other host in the fleet transparently proxies through to it —
  **not** by pointing `EJABBERD_API_URL` at that remote host's IP directly. Keeping
  `mod_http_api` loopback-only everywhere and routing cross-host traffic through the
  existing hub-token-authenticated proxy (rather than widening ejabberd's own ACL to accept
  a specific remote pyobs-web-admin IP) keeps the security boundary in one place — the proxy
  mechanism — instead of two independently-configured ones.

This also means only one instance in the whole fleet needs `EJABBERD_ENABLED = True` +
correctly pointed `EJABBERD_HOST`/`EJABBERD_DOMAIN`/`EJABBERD_API_URL` (whichever one
actually runs it); every other instance just needs `EJABBERD_ENABLED = True` and
`EJABBERD_HOST` set to that instance's `HUB_HOSTS` name to see the same data.

### Where it surfaces

Per the user's split — dashboard for the fleet-wide picture, module pages for the
per-module detail — rather than folding this into the ACL matrix (that stays config-only,
static; this is live server state):

- **Dashboard**: a summary tile (connected count / registered count / node status) in the
  same row as the existing Total/Running/Stopped/RAM/CPU tiles, plus a small "XMPP
  connected" indicator per module row — but **only for modules `get_comm_user` resolves a
  name for**. A module with no `comm:` block at all (confirmed real example: `HttpFileCache`)
  was never going to connect in the first place, so there's no "should be connected but
  isn't" mismatch to surface for it — its own process-status dot already fully describes its
  health. The indicator only exists to compare against modules that config says *should*
  have an XMPP session.
- **Module detail page**: same gate — the ejabberd stat block only appears at all if that
  module has a `comm.user`. When it does: connected-since/IP/resource if live
  (`user_sessions_info`), last-seen if not (`get_last`), and a registered-or-not check
  (`check_account`) to distinguish "not connected right now" from "this account doesn't even
  exist." Natural home: a new stat block in the existing Overview tab, alongside PID/uptime/
  memory/CPU — this is the same kind of "is this module healthy" information, just sourced
  from ejabberd instead of `psutil`.

## Open questions

- ~~Exact `ejabberdctl` output format~~ — **resolved**, see Data layer above: verified against
  a real running instance rather than assumed from documentation.
- ~~Silent-absence vs. visible "not configured"~~ — **resolved: silent.** When
  `EJABBERD_ENABLED` is `False`, the dashboard tile / per-module indicator / module-page
  block are omitted entirely, no "not configured" placeholder anywhere — matching how the
  sidebar's Hosts section only appears when `HUB_HOSTS` is actually configured. (The
  separate *enabled-but-unreachable* case — `EJABBERD_ENABLED = True` yet the query fails —
  still gets the small non-blocking warning proposed above, matching the ACL matrix's
  unreachable-host banner; that's a live failure worth surfacing, not an unconfigured
  feature worth hiding. Flagging this distinction explicitly in case "silent" was meant to
  cover that case too — say so if it should.)
- ~~Refresh cadence~~ — **resolved: `mod_http_api` is fast enough for normal polling, no
  special-cased slower cadence needed.** `ejabberdctl` was measured at ~0.5–0.6s/call (every
  invocation boots a fresh, throwaway Erlang node and connects to the running one via
  distribution protocol — confirmed by reading `/usr/sbin/ejabberdctl` itself, not assumed).
  `mod_http_api`, measured on the same live instance after configuring it (see Data layer),
  came in at **~0.01s/call** — hitting the already-running node directly over HTTP, no new
  VM per call. That's a ~50–60x difference, and it changes the conclusion entirely: the
  dashboard's ejabberd summary tile and per-module indicator can piggyback on the *existing*
  10s status-poll cadence directly, the same way `psutil`-based checks already do, rather
  than needing their own slower, separately-justified schedule. The module page's block can
  still just lazy-load once per tab-open like Config/Logs/ACL, but no longer *needs* to for
  cost reasons — it's a design choice for consistency with those tabs, not a latency
  workaround.
- ~~Comm-user resolution edge cases~~ — **resolved for the no-`comm:` case**: confirmed real
  modules exist with no `comm:` block at all (`HttpFileCache`), and since this app already
  has each module's full resolved config on hand, "does this module even have a `comm.user`"
  is a static, known-in-advance fact, not something that needs runtime probing to guess at —
  `get_comm_user(name) is None` *is* "this module was never expected to connect," full stop,
  and gates the UI accordingly (see "Where it surfaces" above). Still open: whether
  `comm.user` can itself arrive via a shared fragment/anchor the same way `acl:` can (if so,
  `get_comm_user` likely wants the same resolution approach as `get_resolved_acl`, minus the
  provenance tracking since there's no editing use case here).
- ~~Credential layer on top of the loopback ACL~~ — **resolved: not adding one, for v1.**
  Both OAuth and HTTP Basic Auth were actually attempted against the live instance, not just
  discussed — see "Credential layer investigation" in Security model for the full trail
  (OAuth: missing dependency, hard failure; Basic Auth: valid credentials still rejected for
  reasons that resisted diagnosis within reasonable effort, even after matching ejabberd's
  own documented config pattern and ruling out a vhost-mismatch red herring along the way).
  IP-only (`acl: loopback`) is the v1 security model — already fully verified, and sufficient
  for the network-remote-access threat model this feature actually cares about. The residual
  gap (any other local process on the same machine) is accepted, not overlooked.

## Work Plan

- [x] Add `EJABBERD_ENABLED` / `EJABBERD_HOST` / `EJABBERD_DOMAIN` / `EJABBERD_API_URL` / `EJABBERDCTL` settings. → `pyobs_web_admin/settings.py`.
- [ ] Document the ejabberd-side config (listener `request_handlers` + `modules` + `api_permissions`, see Data layer) — this is a real deployment step on the ejabberd side, not just an app setting.
- [ ] `modules/ejabberd.py`: `requests`-based calls to `EJABBERD_API_URL` for the command set above (JSON in, JSON out — no custom text parsing needed, unlike the `ejabberdctl` path); `ejabberdctl` subprocess fallback for hosts without the HTTP API configured. Unit tests against captured real responses from both paths.
- [ ] `services.get_comm_user(name)`: resolve a module's `comm.user` from its config.
- [ ] Local API endpoint(s) exposing ejabberd data, for both direct browser use and hub-proxying (mirrors `/api/acl-matrix/`).
- [ ] Hub-mode delegation: `EJABBERD_HOST == "localhost"` calls `EJABBERD_API_URL` directly, otherwise `proxy.call()` to that host's own endpoint (never point `EJABBERD_API_URL` at a remote host directly — see Hub-mode delegation).
- [ ] Dashboard: summary tile + per-module "XMPP connected" indicator, on the existing 10s status-poll cadence (no longer needs its own slower schedule, see Open questions).
- [ ] Module detail page: session / last-seen / registered-check block in the Overview tab.
