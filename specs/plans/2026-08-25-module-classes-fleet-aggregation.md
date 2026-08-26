# Module classes: fleet aggregation

## Status

Implemented on the pyobs-web-admin side (this repo). Not yet live-verified against a real
two-instance hub pair. Portal-side follow-up filed as pyobs-portal#119, not yet started. See
issue #68.

## Motivation

`api_module_classes` (`GET /api/modules/classes/`, `modules/views.py:753`) was built for
issue #65 so pyobs-portal could filter modules by interface (`ICamera`, `ITelescope`, ...) on
its own side for its script-builder dropdowns. It was deliberately built "always local" —
no `_active_host` proxying, no hub-fleet loop — because the caller was assumed to target one
specific host directly.

Issue #68 found the gap that assumption left: portal only has one `WEBADMIN_URL` configured
and never loops hosts itself, so in practice it only ever sees the one host it's pointed at.
Modules on other hub clients are invisible to it, with no config knob on either side to fix
that. It's the only hub-facing endpoint in this app without a fleet-aggregating counterpart —
every other one (ACL matrix, All Logs, Users) already has one.

## Current state

Three hub-interaction patterns coexist in this app (see `specs/design/index.md`'s "Wide
conventions" section):

1. **Single active-host, session-driven proxying** — `dashboard`, `module_detail`, etc.
2. **Fleet-wide aggregating endpoints** that loop `["localhost"] + HUB_HOSTS` and merge
   server-side. Two flavors already exist:
   - A dumb per-host raw endpoint (`api_acl_matrix`) plus a separate aggregating **page**
     that does the looping/merging (`acl_matrix` view, `services.merge_acl_matrices`,
     `modules/views.py:354`). The split exists because that page needs to render HTML; the
     raw endpoint is what a hub instance calls per sub-host.
   - A single **self-aggregating JSON endpoint** with no separate raw variant:
     `api_all_logs` (`modules/views.py:587`) always loops `["localhost"] + HUB_HOSTS` itself
     and returns merged JSON directly, using `proxy.get_host_config` + `proxy.call`
     (`modules/proxy.py`) and reporting per-host failures via an `unreachable_hosts` list
     rather than failing the whole request.
3. **Raw, always-local, hub-facing endpoints** meant to be queried by another instance doing
   pattern 2's merge — `api_acl_matrix`, `api_module_classes` today, the comm-user-map
   endpoint.

`services.build_module_classes()` (`modules/services.py:2031`) returns a flat
`{module_name: class_fqcn}` dict for the local host only. `api_module_classes` just
`JsonResponse`s that dict unchanged.

pyobs-portal's caller (`pyobs_portal/api/webadmin.py`, `get_module_classes()`) expects exactly
that flat `dict[str, str]` shape, validates it structurally, and caches it 30s.

## Design

**Follow the `api_all_logs` precedent, not the `acl_matrix` one.** `api_module_classes` has no
human-facing page consuming it — its only consumer is a machine (portal) that wants one
answer in one call. The raw+page split exists for acl_matrix because a page needs to query
hosts one at a time to render; there's no equivalent reason to keep a separate "raw" variant
here. So: **extend `api_module_classes` in place** to self-aggregate, no new endpoint, no
`?fleet=1` param. This also composes for nested hubs for free — when a hub instance is asked,
whatever `HUB_HOSTS` *it* has configured gets folded in automatically, exactly like
`api_all_logs` does.

This is a breaking response-shape change for the one known caller, and that's deliberate, not
incidental — see below.

**Response shape must change from a flat dict to a list tagged by host.** A flat
`{module_name: class}` merge silently overwrites on a same-named module across two hosts.
Portal will need the host anyway once it moves from "pick a module for this field" to
"call that module" at execution time, so this isn't just collision-avoidance — it's data the
caller actually needs. New shape:

```json
{
  "modules": [
    {"name": "cam1", "class": "pyobs.modules.camera.BaseCamera", "host": "localhost"},
    {"name": "telescope", "class": "pyobs.modules.telescope.BaseTelescope", "host": "MONETS"}
  ],
  "unreachable_hosts": [{"name": "MONETS", "error": "..."}]
}
```

**Implementation:**

- `services.build_module_classes()` stays unchanged — it's still the correct per-host raw
  shape (flat dict), both for the local branch of the loop and for what a leaf host returns
  when queried by a hub instance's `proxy.call`.
- Add `services.merge_module_classes(per_host: list[tuple[str, dict]]) -> list[dict]`,
  mirroring `merge_acl_matrices` (`modules/services.py:2444`): flatten each host's dict into
  `{name, class, host}` rows. No collision arbitration needed since rows aren't deduped by
  name — a same-named module on two hosts just becomes two distinct rows, disambiguated by
  `host`, same as `merge_acl_matrices` does for ACL rows.
- Rewrite `api_module_classes` (`modules/views.py:753`) to loop
  `["localhost"] + HUB_HOSTS` like `api_all_logs` does: local branch calls
  `services.build_module_classes()` directly; remote branches use
  `proxy.get_host_config` + `proxy.call(host_cfg, "GET", "/api/modules/classes/")` wrapped in
  try/except, appending to `unreachable_hosts` on failure (same pattern as
  `api_all_logs:617-645`). Merge with `merge_module_classes` and return the shape above.
  Update the view's docstring — it no longer describes "always local."
- Update `pyobs-portal`'s `get_module_classes()` (`pyobs_portal/api/webadmin.py`) to parse the
  new `{"modules": [...], "unreachable_hosts": [...]}` shape and return
  `list[dict]`/`None` instead of `dict[str, str]`/`None`, adjusting whatever consumes it
  (`schema.module_ref_options()`) to filter/group by `host` where useful. **This is a
  separate repo and a separate PR** — out of scope for the pyobs-web-admin change itself, but
  must land before or alongside it since the response shape is breaking. Note this explicitly
  in the PR description.

## Open questions

- Confirmed via issue #65/#68: portal is the only known caller of this endpoint. No other
  caller needs the old flat-dict, local-only behavior preserved — grep pyobs-portal before
  merging to be sure nothing else there hits this URL.
- Is `api_module_classes` the only pattern-3 endpoint missing a pattern-2 aggregator? Not
  re-audited here beyond confirming the comm-user-map endpoint already has one (Users page).
  Worth a quick pass over `modules/urls.py`'s remaining `api/*` GETs if this pattern needs
  applying again elsewhere.

## Work Plan

- [x] `services.merge_module_classes` + unit tests in `modules/tests.py` (mirror
      `MergeAclMatricesTests`, `modules/tests.py:771`) — host-tagging, no-collision-arbitration
      behavior, empty-input case
- [x] Rewrite `api_module_classes` to loop + merge, matching `api_all_logs`'s
      unreachable-host handling; update its docstring
- [x] Unit tests for the view: single host (no `HUB_HOSTS`), multi-host merge, one host
      unreachable
- [ ] Verify live against a real two-instance hub pair if available (issue #68 mentions
      `south/monet` / `south/frontend` already share matching `HUB_HOSTS`/`HUB_TOKEN`)
- [ ] Follow-up PR in pyobs-portal: update `get_module_classes()` for the new response shape
      (tracked as pyobs-portal#119)

### Progress log

pyobs-web-admin side implemented: `services.merge_module_classes` added, `api_module_classes`
rewritten to loop `["localhost"] + HUB_HOSTS` and merge (mirroring `api_all_logs`), 7 new unit
tests (4 service-level, 3 view-level) all passing alongside the existing 306. Not yet verified
against a real hub pair. Portal-side change filed as its own issue (pyobs-portal#119) rather
than implemented here, per this doc's note that it's a separate repo/PR.
