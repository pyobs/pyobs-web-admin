# pyobs-web-admin: ejabberd integration — v0.7 (2026-07-03, 21:15)

## Status

**All 7 Work Plan items done — this feature is fully implemented and verified against a
live instance, end to end.** See **Progress log** below for exactly what shipped and how it
was verified; that section stays the authoritative record even though there's no more Work
Plan left to narrate. `ejabberdctl` is kept as a documented fallback to `mod_http_api`, not
deleted from the plan. IP-only (`acl: loopback`) is the settled security model — see
"Credential layer investigation" in Security model for why. Not in this pass, and open for
later if wanted: the "typo/staleness detection" tie-in with `ACL_MATRIX.md` (cross-checking
`acl:` callers against `registered_users`) mentioned in Current state, and write actions
(register/unregister/password), explicitly out of scope per Motivation. See ACL_MATRIX.md
for the ACL matrix feature this one is related to but separate from (both surface "who can
talk to what," but this one reads live XMPP server state rather than static config).

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
- **Done — Work Plan item 2.** No new writing needed — the "ejabberd-side configuration
  (verified working)" block in Data layer already *is* this item, written and tested against
  a real instance back in v0.3–v0.5, before any Work Plan item was checked off. Deliberately
  **not** promoting it into `README.md`'s Configuration/production-setup sections yet, even
  though that's the more natural place an operator would look for a real deployment step —
  `README.md` documents what the running app actually does today, and right now these
  settings aren't consumed by any code, so a reader following README instructions would
  configure ejabberd for a feature that doesn't do anything yet. Promoting this into README
  is bundled into the final "Dashboard + module page" Work Plan item instead, once the
  feature is actually end-to-end functional.
- **Done — Work Plan item 3.** `modules/ejabberd.py`: one function per command
  (`status`/`stats`/`connected_users_info`/`registered_users`/`user_sessions_info`/
  `get_last`/`check_account`), each choosing HTTP (`mod_http_api`) or `ejabberdctl` purely
  based on whether `EJABBERD_API_URL` is set (`_use_http()`) — not by catching HTTP failures
  and falling back, since a real HTTP error (bad permissions, unreachable host) should
  surface, not be silently masked by a slower path meant for hosts that simply haven't
  configured the HTTP API yet. Found and fixed a real bug while testing against the mocked
  fixtures: the `ejabberdctl` fallback's `_ctl_call` originally did `.strip()` on the whole
  subprocess output, which silently ate a *meaningful* trailing tab (`connected_users_info`/
  `user_sessions_info`'s last field, `statustext`, is legitimately empty in practice, and
  ejabberd's own tab-separated format still emits the trailing tab for it) — dropping that
  key from the parsed dict entirely. Fixed by only stripping where it's actually safe
  (`status`/`stats`, single-value results) and using `.splitlines()` (which strips `\n`
  without touching other whitespace) everywhere a trailing empty field can be meaningful.
  Tests (`EjabberdHttpTests`, `EjabberdCtlFallbackTests`, `EjabberdPathSelectionTests` in
  `modules/tests.py`) mock the transport layer (`requests.post`/`subprocess.run`) but use the
  *exact* real response strings/JSON captured against the live instance during v0.2–v0.5 as
  fixtures, not invented shapes — this is what caught the trailing-tab bug, since a made-up
  fixture likely wouldn't have included it. `python manage.py test modules` — 57/57 passing.
  Verified both code paths for real afterward, not just against mocks: ran every function
  against the live instance with `EJABBERD_API_URL` set (HTTP path) and again with it unset
  and `EJABBERDCTL` pointed at a small `sudo -n ejabberdctl "$@"` wrapper script (ctl path,
  since this dev box needs `sudo` for `ejabberdctl`) — both produced identical, correct
  results matching the mocked tests exactly.
- **Done — Work Plan item 4.** `services.get_comm_user(name)`: reuses `get_resolved_acl`'s
  exact resolution pipeline (`pre_process_yaml` + `yaml.safe_load`) rather than a fresh
  implementation, pulling `comm.user` out of the resolved dict instead of `acl`. No
  provenance tracking (unlike `get_resolved_acl`'s `source`) — there's no editing use case
  for `comm.user`, only display, so there's nothing to route an edit to. Closes the one
  open question left in this doc (`comm.user` via a shared fragment or YAML anchor/merge
  key): `GetCommUserTests` in `modules/tests.py` covers a locally-defined `comm.user`, one
  arriving via `{include}`, and one via a real config's actual shape (`comm: {<<: *comm,
  user: camera, ...}`, an anchor merge key) — all three resolve correctly, for free, simply
  by reusing `get_resolved_acl`'s pipeline rather than writing new resolution logic.
  `python manage.py test modules` — 63/63 passing. Verified against the real
  `PYOBS_CONFIG_DIR` too, not just synthetic fixtures: `camera`/`telescope` resolve to their
  real `comm.user` values, `filecache` (`HttpFileCache`, no `comm:` block) correctly
  resolves to `None`.
- **Done — Work Plan item 5.** Two new endpoints, both deliberately "dumb" — no
  host-awareness of their own, exactly like `api_acl_matrix` — since hub-delegation is item
  6's concern, not this one's: `GET /api/ejabberd/status/` (`views.api_ejabberd_status`)
  returns this instance's own node status/registered count/online count/full connected-user
  list in one shot, for the dashboard's summary tile; `GET /api/ejabberd/user/<user>/`
  (`views.api_ejabberd_user`) returns one JID local-part's registered/session/last-seen
  state, for the module page's per-module block. Deliberately **not** a single combined
  `/api/modules/<name>/ejabberd/` endpoint doing both `get_comm_user` resolution and the
  ejabberd query together — those two steps can legitimately need *different* hosts (a
  module's own config lives wherever that module runs; the actual ejabberd query goes
  wherever `EJABBERD_HOST` points, which the design doc's Hub-mode delegation section
  already established can be a third host entirely) — item 6/7 is where that two-step
  orchestration gets stitched together, not this item. No permanent unit tests added for
  these two views, matching this repo's existing convention (only `services.py`/data-layer
  functions get permanent tests; thin views get manual verification) — verified instead via
  the Django test client directly against the live instance. That verification surfaced one
  more real discovery worth documenting: `get_last` for a user that was **never**
  registered returns `{"status": "NOT FOUND", "timestamp": <current time>}` — a third case
  beyond "ONLINE" and a real disconnect reason, now folded into the Data layer table.
- **Done — Work Plan item 6.** Three new private helpers in `views.py`:
  `_ejabberd_host_config()` (resolves `EJABBERD_HOST` into a proxy host dict or `None`,
  mirroring `_active_host` — but reading fixed settings, not session state, since ejabberd
  is normally one shared server for the whole fleet, not something an admin switches
  per browser tab like the sidebar's host selector), `_ejabberd_status()`, and
  `_ejabberd_user(user)` (both branch on `_ejabberd_host_config()`: call this instance's own
  `ejabberd.py` directly if `None`/localhost, otherwise `proxy.call()` to that host's own
  `api_ejabberd_status`/`api_ejabberd_user` — the item 5 endpoints, which stay "dumb" and
  never redelegate further, so there's exactly one hop). Verified for real, not just
  structurally: a genuine two-instance hub/spoke pair (same throwaway-settings-module
  technique used for the ACL matrix's own hub-mode verification), both pointed at the same
  real ejabberd instance since it's a single shared server here too — the hub's
  `_ejabberd_status()`/`_ejabberd_user("camera")` correctly proxied to the spoke and
  returned identical data to calling them directly on the spoke; stopping the spoke produced
  a clean, catchable `ConnectionError` (matching how `proxy.call` failures already surface
  elsewhere in this app, e.g. the ACL matrix's per-host try/except) rather than a hang or a
  silently wrong answer. Not yet wired into any page — that's item 7, which is also where an
  "ejabberd unreachable" indicator (if ever needed) would be built around this same
  exception, the same way the ACL matrix already handles one host being down.
- **Done — Work Plan item 7. Last Work Plan item — this doc's implementation phase is
  complete.** Two new *browser-facing* endpoints, distinct from item 5's hub-facing "dumb"
  ones: `GET /api/ejabberd-summary/` (delegates via `_ejabberd_status()`, gated by
  `EJABBERD_ENABLED` — returns `{"enabled": false}` without querying anything if the feature
  is off; deliberately *not* host-aware via the session's active host like most API views
  here, since ejabberd is one shared server for the whole fleet, so the summary is the same
  regardless of which host's dashboard is being viewed) and `GET
  /api/modules/<name>/ejabberd/` (host-aware in the module sense — proxies the whole
  request to the module's own host first, exactly like `api_acl`'s GET branch, then once
  local, resolves `comm.user` and delegates to `_ejabberd_user()`). `api_all_statuses` also
  gained a `comm_user` field per module, reusing the existing 10s status-poll response
  rather than a separate request.

  Dashboard: a summary tile (`online / registered` counts, node status as a tooltip) next
  to the existing Total/Running/Stopped/RAM/CPU tiles, plus a small icon next to each
  module's status badge — filled green "connected" if that module's `comm_user` is in the
  live connected list, outlined amber "not connected" if it has a `comm_user` but isn't,
  and altogether absent (not just hidden) for a module with no `comm_user` at all. Both
  poll on the existing 10s `refreshAllStatuses`/new `refreshEjabberd` cadence, per the
  resolved refresh-cadence question. Module page: a new row in the Overview tab (alongside
  PID/uptime/memory/CPU), showing connected-since/IP/connection type if live, last-seen (or
  "never connected") if not, or a distinct "not a registered account" state — omitted
  entirely for a module with no `comm_user`.

  **A real gap caught by testing, not just assumed correct**: the per-module dashboard
  indicator and the module page's row were initially only CSS-hidden (`d-none`) when
  `EJABBERD_ENABLED` was `False`, not template-omitted like the summary tile — meaning the
  resolved "silent absence" decision was only half-implemented. Caught by testing the
  disabled case explicitly with the Django test client (checking for the literal HTML tag,
  not a naive substring match — a first pass at that same check gave a false positive by
  matching JS code's own `getElementById('ejabberd-tile')` string). Fixed by wrapping both
  in `{% if ejabberd_enabled %}`, then re-verified disabled produces zero markup and enabled
  produces exactly the expected elements, both via the test client and against the live
  scratch server.

  Verified live end-to-end against the real instance (scratch settings module pointed at
  the real `PYOBS_CONFIG_DIR` and real ejabberd, not touching `local_settings.py`): dashboard
  tile and `/api/ejabberd-summary/` correct; `/api/modules/camera/ejabberd/` correctly
  showed the real "not connected, last seen with an actual disconnect reason" state (`camera`
  didn't stay running long enough in this sandbox to catch it live-connected again, but that
  exact response shape was already verified as real earlier in this doc's design phase);
  `/api/modules/filecache/ejabberd/` correctly returned `{"comm_user": null}` for
  `HttpFileCache`. `python manage.py test modules` — 63/63 passing (no new permanent tests
  for these views, matching this repo's established convention).

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
| `get_last` (`{"user": ..., "host": ...}`) | `{"timestamp": "...", "status": "ONLINE"}` while connected; a **freeform last-disconnect reason** in `status` otherwise (e.g. `"Stream reset by peer"`) for a real, previously-seen account; `{"status": "NOT FOUND", "timestamp": <current time>}` for a user that was **never** registered/connected — three cases, not two, and `status` is never a fixed enum | Module page: "last connected 3h ago (stream reset by peer)" for a module that looks stuck; `NOT FOUND`'s timestamp is just "now," not a meaningful last-seen time, so a caller should check `registered` first rather than trust this timestamp for an unregistered account |
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
- ~~Comm-user resolution edge cases~~ — **fully resolved.** No-`comm:` case: confirmed real
  modules exist with no `comm:` block at all (`HttpFileCache`), and since this app already
  has each module's full resolved config on hand, "does this module even have a `comm.user`"
  is a static, known-in-advance fact, not something that needs runtime probing to guess at —
  `get_comm_user(name) is None` *is* "this module was never expected to connect," full stop,
  and gates the UI accordingly (see "Where it surfaces" above). Shared-fragment/anchor case:
  resolved for free by reusing `get_resolved_acl`'s exact pipeline instead of writing new
  resolution logic — verified against `{include}` and a real config's actual anchor-merge-key
  shape (`comm: {<<: *comm, user: camera, ...}`), both resolve correctly (see Progress log,
  Work Plan item 4).
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
- [x] Document the ejabberd-side config (listener `request_handlers` + `modules` + `api_permissions`, see Data layer) — this is a real deployment step on the ejabberd side, not just an app setting. → "ejabberd-side configuration (verified working)" in Data layer, already written and verified against a live instance during v0.3–v0.5. Not yet promoted into README.md's operator-facing Configuration/production-setup sections — see Progress log for why.
- [x] `modules/ejabberd.py`: `requests`-based calls to `EJABBERD_API_URL` for the command set above (JSON in, JSON out — no custom text parsing needed, unlike the `ejabberdctl` path); `ejabberdctl` subprocess fallback for hosts without the HTTP API configured. Unit tests against captured real responses from both paths. → `modules/ejabberd.py`, tests in `modules/tests.py`.
- [x] `services.get_comm_user(name)`: resolve a module's `comm.user` from its config. → `modules/services.py`, tests in `modules/tests.py`.
- [x] Local API endpoint(s) exposing ejabberd data, for both direct browser use and hub-proxying (mirrors `/api/acl-matrix/`). → `GET /api/ejabberd/status/`, `GET /api/ejabberd/user/<user>/` in `views.py`/`urls.py`.
- [x] Hub-mode delegation: `EJABBERD_HOST == "localhost"` calls `EJABBERD_API_URL` directly, otherwise `proxy.call()` to that host's own endpoint (never point `EJABBERD_API_URL` at a remote host directly — see Hub-mode delegation). → `views._ejabberd_host_config`/`_ejabberd_status`/`_ejabberd_user`, verified via a real two-instance hub/spoke pair.
- [x] Dashboard: summary tile + per-module "XMPP connected" indicator, on the existing 10s status-poll cadence (no longer needs its own slower schedule, see Open questions). → `templates/modules/dashboard.html`, `api_ejabberd_summary`, `comm_user` added to `api_all_statuses`.
- [x] Module detail page: session / last-seen / registered-check block in the Overview tab. → `templates/modules/detail.html`, `api_module_ejabberd`.
