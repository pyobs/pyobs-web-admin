# pyobs-web-admin: development index

## Status

Meta/index doc, not a feature doc itself. Points at each feature's own design doc and
collects ideas that haven't been fleshed out into one yet.

## How this works

Each non-trivial feature gets its own `FEATURE_NAME.md` at the repo root, following the
shape `ACL_MATRIX.md`/`EJABBERD_INTEGRATION.md` already use: **Status** (one-paragraph
current state, updated as work progresses) → **Motivation** → **Current state** (what's true
in the codebase before this feature) → **Design** → **Open questions** → **Work Plan**
(checkboxes, narrated by a **Progress log** as items land). Code comments referencing "the
design doc" should name the specific file (`ACL_MATRIX.md`, not `DEVELOPMENT.md`) so they
keep pointing at the right document as more of these accumulate.

An idea starts as a one-line bullet under **Ideas** below. When someone's ready to actually
work on it, it gets promoted to its own doc (even a short v0.1 sketch is enough to start —
see `EJABBERD_INTEGRATION.md`'s early versions) and gets a line in **Feature docs** instead,
linking back here. Ideas don't need to be fully thought through to get listed — half-formed
is fine, that's what the Design section of the eventual doc is for.

## Feature docs

- [ACL_MATRIX.md](ACL_MATRIX.md) — fleet-wide ACL matrix page: view every module's `acl:`
  policy in one table, edit it via a structured form (matrix modal or a per-module tab),
  aggregated across hub hosts. **Core (view/edit/hub-aggregation) shipped.** Groups/profiles
  (named caller-list reuse) paused by deliberate choice, not started.
- [EJABBERD_INTEGRATION.md](EJABBERD_INTEGRATION.md) — read-only visibility into ejabberd's
  own state (registered/connected users, per-module session info) on the dashboard and
  module pages, closing the "process running ≠ XMPP connected" and "config vs. reality" gaps.
  **Shipped, all 7 Work Plan items done and verified against a live instance** (including
  the `mod_http_api` config and its security model).
- [JOURNALD_LOGS.md](JOURNALD_LOGS.md) — one switch (`PYOBS_LOG_BACKEND`) that both starts
  pyobs modules logging into the systemd journal (`pyobs --syslog`, already supported
  upstream) instead of a flat file, and reads them back from there for the existing log
  viewer. **Implemented and verified live end-to-end.** Only one deploy-time question left
  open (whether a genuinely group-less service account needs an explicit `journalctl`
  permission grant), see that doc's Status.
- [EJABBERD_USER_MANAGEMENT.md](EJABBERD_USER_MANAGEMENT.md) — register/reset-password/ban/
  unregister XMPP accounts for a module's `comm.user` from pyobs-web-admin, closing the
  write-side gap `EJABBERD_INTEGRATION.md` deliberately left open. **Implemented and
  verified live end-to-end** (full register → reset-password → ban → unban → unregister
  round trip against a real ejabberd instance, using a disposable test account and a scratch
  module config, never a real one). `ejabberdctl`-only transport needed no ejabberd-side ACL
  change; config write-back handles a `comm.user` shared across modules (confirmed against a
  copy of this box's real config). Hub-mode delegation is code-complete but not yet driven
  against a real two-instance pair the way the read path was. Only `README.md` is left.
  Also shipped a fleet-wide, read-only **Users page** (`/xmpp-users/`) on top of this --
  every registered account across all hub hosts, cross-referenced against every module's
  `comm.user`, with a running-status dot disambiguating which module owns a shared identity's
  live session. Deliberately no write actions there yet, see Ideas below.

## Ideas (not yet designed)

- Two dashboards rather than making the existing one fleet-wide: keep today's Dashboard as a
  per-host operational control surface (Start All/Stop All and per-module quick actions make
  more sense scoped to one host at a time — a fleet-wide "Stop All" from one button is a real
  footgun), and add a *separate*, lighter fleet-wide overview page (which hosts are up,
  aggregate counts, no bulk actions) closer in spirit to `ACL_MATRIX.md`/"All Logs". Sidebar
  nav already treats Dashboard as a global entry (listed above the Hosts section) even though
  the view itself (`modules/views.py:dashboard`) still switches to the single active host --
  that mismatch was the original motivation, before landing on two-pages-not-one instead of
  converting the existing page. Note this would be a third nav pattern in this app (today:
  "always per-host" like module pages, or "always fleet-wide" like ACL Matrix -- this adds
  "both, separately").
- Write actions directly on the Users page (`/xmpp-users/`), not just links out to each
  identity's module page: buttons for register, ban/unban, unregister, and change-password
  right in the table. Needs its own design pass, not just wiring up the existing
  `api_module_ejabberd_*` endpoints, because the Users page's whole premise is showing
  accounts a module page can't: an identity with **no** owning module (e.g. `admin`) has
  nowhere to route "register" to (that action reads the password from a module's own
  `comm.password:` — there's no config to read it from), and an identity shared by **several**
  modules needs either a module picker or a per-row action scoped to whichever module the
  click came from. `EJABBERD_USER_MANAGEMENT.md`'s existing tiered confirmation (simple
  dialog vs. retype-to-confirm for `unregister`) should carry over unchanged either way.

## Wide (not per-feature) conventions worth knowing before touching any feature doc

- No database — sessions are signed cookies (`SESSION_ENGINE` in `pyobs_web_admin/settings.py`).
  Any feature needing persisted app-local state (not `pyobs` config, not session data) needs
  its own storage decision, documented in that feature's own doc (see `ACL_MATRIX.md`'s
  Groups section for one such call already made: a flat JSON file, not a new DB dependency).
- Hub mode (`HUB_HOSTS` in settings, `modules/proxy.py`) is normally "one active host at a
  time" (dashboard/config/logs switch to whichever host the sidebar has selected) — a feature
  that instead needs to aggregate *every* host on one page (like the ACL matrix) is the
  exception, not the default, and should call that out explicitly in its own doc rather than
  assume the reader already knows which model applies.
- Tests live in `modules/tests.py`, plain `unittest.TestCase` (not Django's `TestCase` —
  there's no database to wrap transactions around). Run with `python manage.py test modules`.
