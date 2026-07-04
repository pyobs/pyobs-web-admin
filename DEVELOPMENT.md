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
  write-side gap `EJABBERD_INTEGRATION.md` deliberately left open. **Design settled, write
  commands verified live against a disposable test account (no application code yet).** Full
  command scope, `ejabberdctl`-only transport (no ejabberd-side ACL change needed), tiered
  confirmation, and automatic config write-back (including the shared-`comm.user`-across-
  modules case) are all decided; live verification also caught real surprises worth knowing
  before implementing (`check_password` crashes on a banned account, `unregister` is silently
  idempotent on a nonexistent user) — see that doc's Current state.

## Ideas (not yet designed)

- Make the Dashboard fleet-wide (aggregate modules across all hub hosts, like `ACL_MATRIX.md`
  and the "All Logs" view do) instead of only showing whichever host is currently active. Sidebar
  nav already treats Dashboard as a global entry (listed above the Hosts section), but the view
  itself (`modules/views.py:dashboard`) still switches to the single active host — that mismatch
  is the motivation.

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
