# pyobs-web-admin: development index

## Status

Meta/index doc, not a feature doc itself. Points at each feature's own design doc and
collects ideas that haven't been fleshed out into one yet.

## How this works

Each non-trivial feature gets its own `FEATURE_NAME.md` at the repo root, following the
shape `DEV_ACL_MATRIX.md`/`DEV_EJABBERD_INTEGRATION.md` already use: **Status** (one-paragraph
current state, updated as work progresses) → **Motivation** → **Current state** (what's true
in the codebase before this feature) → **Design** → **Open questions** → **Work Plan**
(checkboxes, narrated by a **Progress log** as items land). Code comments referencing "the
design doc" should name the specific file (`DEV_ACL_MATRIX.md`, not `DEVELOPMENT.md`) so they
keep pointing at the right document as more of these accumulate.

An idea starts as a one-line bullet under **Ideas** below. When someone's ready to actually
work on it, it gets promoted to its own doc (even a short v0.1 sketch is enough to start —
see `DEV_EJABBERD_INTEGRATION.md`'s early versions) and gets a line in **Feature docs** instead,
linking back here. Ideas don't need to be fully thought through to get listed — half-formed
is fine, that's what the Design section of the eventual doc is for.

## Feature docs

- [DEV_ACL_MATRIX.md](DEV_ACL_MATRIX.md) — fleet-wide ACL matrix page: view every module's `acl:`
  policy in one table, edit it via a structured form (matrix modal or a per-module tab),
  aggregated across hub hosts. **Core (view/edit/hub-aggregation) shipped.** Groups/profiles
  (named caller-list reuse) was fully designed and implemented, then reverted at explicit
  request — see [DEV_ACL_GROUPS.md](DEV_ACL_GROUPS.md), moved out of this doc entirely.
- [DEV_EJABBERD_INTEGRATION.md](DEV_EJABBERD_INTEGRATION.md) — read-only visibility into ejabberd's
  own state (registered/connected users, per-module session info) on the dashboard and
  module pages, closing the "process running ≠ XMPP connected" and "config vs. reality" gaps.
  **Shipped, all 7 Work Plan items done and verified against a live instance** (including
  the `mod_http_api` config and its security model).
- [DEV_JOURNALD_LOGS.md](DEV_JOURNALD_LOGS.md) — one switch (`PYOBS_LOG_BACKEND`) that both starts
  pyobs modules logging into the systemd journal (`pyobs --syslog`, already supported
  upstream) instead of a flat file, and reads them back from there for the existing log
  viewer. **Implemented and verified live end-to-end.** Only one deploy-time question left
  open (whether a genuinely group-less service account needs an explicit `journalctl`
  permission grant), see that doc's Status. `PYOBS_LOG_BACKEND` now defaults to auto-detecting
  from `pyobsd`'s own config file (`pyobs-core`'s daemon manager) instead of requiring it set
  a second time — an explicit setting still overrides. Also gained pagination: both log
  windows (a module's own Logs tab, and fleet-wide All Logs) now auto-load older entries when
  scrolled to the top, via journalctl's `--until` — journald-backed modules only, since the
  file backend has no seek/offset to page further back with.
- [DEV_EJABBERD_USER_MANAGEMENT.md](DEV_EJABBERD_USER_MANAGEMENT.md) — register/reset-password/ban/
  unregister XMPP accounts for a module's `comm.user` from pyobs-web-admin, closing the
  write-side gap `DEV_EJABBERD_INTEGRATION.md` deliberately left open. **Implemented and
  verified live end-to-end** (full register → reset-password → ban → unban → unregister
  round trip against a real ejabberd instance, using a disposable test account and a scratch
  module config, never a real one). `ejabberdctl`-only transport needed no ejabberd-side ACL
  change; config write-back handles a `comm.user` shared across modules (confirmed against a
  copy of this box's real config). Hub-mode delegation is code-complete but not yet driven
  against a real two-instance pair the way the read path was. Only `README.md` is left.
  Also shipped a fleet-wide **Users page** (`/xmpp-users/`) on top of this -- every
  registered account across all hub hosts, cross-referenced against every module's
  `comm.user`, with a running-status dot disambiguating which module owns a shared identity's
  live session, plus write-action buttons: register is per-module (uses that module's own
  `comm.password:` -- two modules sharing an identity can have different passwords before
  either is registered, verified live), reset-password/ban/unregister are row-level and
  bare-username-scoped (no owning module required, so accounts like `admin` are actionable
  too) via new endpoints separate from the module-scoped ones. Also a standalone manual
  "register account" form (username + password typed directly, no config to source from) for
  a module running entirely outside this fleet. Plus a **Kick** action --
  `ejabberd.kick_session` (not `kick_user`, which takes no reason at all) with a fixed,
  greppable reason (`"Kicked via pyobs-web-admin"`), so the module side can distinguish an
  intentional admin kick from any other disconnect. Verified live against a real running
  `camera` module, twice, including confirming a second Claude session's pyobs-core change
  that (a) shuts the module down instead of reconnecting when the XMPP stream error is
  `conflict` specifically (a genuine identity takeover, distinct from e.g. `system-shutdown`,
  which should still retry), and (b) now logs the actual kick reason text instead of a
  hardcoded message. UI: no modals anywhere on the page (inline expand-in-place confirms and
  an always-visible register form instead, for mobile-friendliness), collapsed accordion-style
  rows (any number open at once, plus an Expand-all toggle) rather than a wide table.
- **Two dashboards** (no separate design doc — small enough to build directly from the idea
  below plus a short back-and-forth on scope). Today's Dashboard (`/`) stays exactly as it
  was, a per-host operational control surface (Start All/Stop All, per-module quick actions).
  New: a fleet-wide **Overview** page (`/overview/`, sidebar entry above Logs/ACL Matrix/Users)
  — one row per configured host (aggregated the same way as `acl_matrix`/`all_logs`/
  `xmpp_users`: every `HUB_HOSTS` entry queried via the existing `/api/statuses/` endpoint,
  unreachable hosts shown as a warning banner and excluded from the table rather than shown
  with an error row), with running/stopped/total counts and aggregate CPU/RAM per host
  (`views._host_summary`, summing each module's own `get_module_stats`), and the host's name
  linking into *that host's own* per-host Dashboard (`_cross_host_url`, generalized to accept
  no `arg` for URLs like `dashboard` that take none). Deliberately **no bulk or per-module
  actions at all** on this page, not even individual ones — it's a pure summary, exactly the
  footgun-avoidance the idea below was about; anyone wanting to act on a module goes to that
  host's own Dashboard via the row's link. Verified live against the real fleet (one
  unreachable `HUB_HOSTS` entry, `MONETS`, correctly banner'd and excluded; `localhost`'s real
  counts confirmed correct) and at a 390px mobile viewport (needed one fix: `white-space:
  nowrap` on the table cells, since without it a tight viewport wrapped cell text like
  "511.7 MB" mid-word instead of properly triggering `table-responsive`'s horizontal scroll —
  confirmed fixed by scrolling the container programmatically and screenshotting the RAM
  column coming into view). Also moved Dashboard's own sidebar link to sit below the Hosts
  section (previously listed above it as if global, despite the view itself always being
  per-host — the original mismatch that prompted this whole idea).
- **New module button.** `services.create_module(name)` — the one path allowed to write a
  `.yaml` file that doesn't exist yet (unlike `save_config`, which explicitly refuses to,
  `raise FileNotFoundError`), writing a minimal starter (`# class: pyobs.modules.<package>.
  <ClassName> -- ...` comment + a bare `class:` key). Surfaces as a small "+" icon next to the
  sidebar's "Modules" header, linking to a dedicated page (`/modules/new/`, not a modal — this
  app's established mobile-friendliness convention) with a single name input; on success,
  navigates straight to the new module's own Config tab (`#tab-config`) to fill in the rest.
  New endpoint `POST /api/modules/create/` follows the session's active host exactly like
  `api_config` (proxies to a remote host's own identical endpoint if one is active, since
  hub-token-authenticated requests execute locally there with no active-host session of their
  own — same pattern, no special-casing needed). `_get_module_or_404`-style validation reuses
  the existing `validate_name` regex; refuses (409) if the name already exists rather than
  clobbering it. Unit tests in `modules/tests.py` (`CreateModuleTests`): starter content,
  invalid-name rejection, already-exists rejection (confirms the existing file survives
  untouched), and config-dir-auto-created-if-missing. Verified live end-to-end against a
  scratch `PYOBS_CONFIG_DIR`: clicked the sidebar "+", typed a name, landed on the new module's
  Config tab with the starter YAML on disk; separately confirmed both error paths (duplicate
  name, invalid name) render inline, and checked the form page at a 390px mobile viewport
  (clean, no overflow).

## Ideas (not yet designed)

None currently.

## Wide (not per-feature) conventions worth knowing before touching any feature doc

- No database — sessions are signed cookies (`SESSION_ENGINE` in `pyobs_web_admin/settings.py`).
  Any feature needing persisted app-local state (not `pyobs` config, not session data) needs
  its own storage decision, documented in that feature's own doc (see `DEV_ACL_GROUPS.md` for one
  such call already made: a flat JSON file, not a new DB dependency).
- Hub mode (`HUB_HOSTS` in settings, `modules/proxy.py`) is normally "one active host at a
  time" (dashboard/config/logs switch to whichever host the sidebar has selected) — a feature
  that instead needs to aggregate *every* host on one page (like the ACL matrix) is the
  exception, not the default, and should call that out explicitly in its own doc rather than
  assume the reader already knows which model applies.
- Tests live in `modules/tests.py`, plain `unittest.TestCase` (not Django's `TestCase` —
  there's no database to wrap transactions around). Run with `python manage.py test modules`.
