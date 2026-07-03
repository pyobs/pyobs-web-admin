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
  **Design verified against a live instance** (including the `mod_http_api` config and its
  security model); no implementation yet.

## Ideas (not yet designed)

_Nothing queued yet — add a one-line bullet here when one comes up._

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
