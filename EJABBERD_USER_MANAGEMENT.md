# pyobs-web-admin: ejabberd user management — v1.0 (2026-07-04)

## Status

**Implemented and verified live end-to-end.** All Work Plan items are done: `get_comm_user`
source-tracking, the five `modules/ejabberd.py` write functions plus `get_ban_details`,
`services.py`'s shared-`comm.user` handling and config write-back, the module page's tiered
confirmation UI, and hub-mode delegation. Verified against a real ejabberd 24.12-4 instance
using a disposable test account and a scratch module config (never a real module or account)
— register → check → reset password (config write-back confirmed byte-for-byte against the
live account) → ban → unban → unregister, plus the shared-identity warning with a second
module sharing the same identity, plus an error-path probe (register with no `comm.password:`
configured). Only remaining Work Plan item is `README.md`, deliberately deferred until now
that everything above is actually true. Not yet exercised live: a genuine two-instance
hub/spoke pair for the write actions specifically (the delegation code mirrors the read
path's already-verified shape, but hasn't itself been driven end-to-end across two real
instances the way `EJABBERD_INTEGRATION.md`'s reads were).

## Motivation

`EJABBERD_INTEGRATION.md` gave this app read-only visibility into ejabberd (who's registered,
who's connected, last-seen) and explicitly scoped write actions out of that doc: "much higher
blast radius than a read-only status view — accidentally locking out a production XMPP account
mid-observation." This doc is that deferred follow-up.

The gap this closes: today, giving a module's `comm.user` a working XMPP account — or fixing
one that's broken — means leaving `pyobs-web-admin` entirely to run `ejabberdctl` (or ejabberd's
own web admin UI) by hand. The read-only integration can already *tell* an operator that a
module's `comm.user` isn't a registered account at all (`check_account`, already surfaced on the
module page's Overview tab) — it just can't *fix* that from the same page. This doc closes that
specific loop: register/reset/remove an XMPP account without leaving this app.

## Current state

- `modules/ejabberd.py` has one function per **read** command only
  (`status`/`stats`/`connected_users_info`/`registered_users`/`user_sessions_info`/`get_last`/
  `check_account`), each branching on `_use_http()` between `mod_http_api` (HTTP, ~0.01s/call)
  and an `ejabberdctl` subprocess fallback (~0.5–0.6s/call) — see `EJABBERD_INTEGRATION.md`,
  Data layer. No write functions exist yet; confirmed by reading the file directly.
- ejabberd's own `api_permissions` config (the loopback-only `mod_http_api` grant
  `EJABBERD_INTEGRATION.md` documents and verified live) whitelists exactly seven read commands
  in its `what:` list — `register`/`unregister`/`change_password`/`ban_account`/`unban_account`
  are not in it. **Doesn't matter for this doc's settled design** — see Design's Transport
  decision: writes go through `ejabberdctl`, not `mod_http_api`, and that same config already
  has a separate `"console commands"` entry (`from: [ejabberd_ctl], who: all, what: "*"`)
  granting full access to anything invoked via `ejabberdctl` — so no ejabberd-side config change
  is needed for this feature at all, unlike the original `mod_http_api` setup.
- Real command signatures, read from `ejabberdctl help <command>` on the live instance (an
  informational, read-only call — not a write):
  - `register user host password` → result tagged `restuple`
  - `unregister user host` → result tagged `restuple`. ejabberdctl's own help text: *"This
    deletes the authentication and all the data associated to the account (roster, vcard...)"*
    — permanent, not reversible.
  - `change_password user host newpass` → result tagged `rescode` (ejabberdctl's own example
    shows a bare `'ok'` on success)
  - `ban_account user host reason` / `unban_account user host` — ejabberd's own account-banning
    pair, surfaced by `ejabberdctl help accounts` and not in `DEVELOPMENT.md`'s original idea
    bullet. `ejabberdctl help`'s own description ("reversible") turned out to undersell what's
    actually happening — see "Verified live" below.

### Verified live — every write command exercised, not assumed

Run against the real instance via `ejabberdctl` (never `mod_http_api`, per the Transport
decision), using a disposable account (`docverifytest99`) created and fully removed for this
purpose — `registered_users localhost` confirmed back to exactly the original six real
accounts (`admin`/`camera`/`mastermind`/`observer`/`scheduler`/`telescope`) afterward, no
residue. This directly follows `EJABBERD_INTEGRATION.md`'s own precedent (its "Credential layer
investigation" did the same register-test-unregister dance for the same reason) and its own
warning against trusting a command's help text or docs over its actual behavior (the
trailing-tab bug).

**A first pass at this table wrongly attributed the error messages below to stderr** — an
artifact of testing with `2>&1` (merged streams), which can't actually distinguish which
stream produced what. Redone with `stdout`/`stderr` captured to separate files: **every
message below, success or failure, is on stdout — `ejabberdctl` never writes to stderr for any
of these commands.** The exit code is the only reliable success/failure signal;
`modules/ejabberd.py`'s existing `_ctl_call` already reflects this by only ever capturing
`stdout`, but the new write functions must check `returncode` too, which `_ctl_call` currently
discards.

| Command | Success | Failure |
|---|---|---|
| `register user host pw` | Exit `0`, stdout `"User <user>@<host> successfully registered"` | Exit `1`, stdout `"Error: conflict: User <user>@<host> already registered"` (registering an existing user) |
| `change_password user host newpass` | Exit `0`, **stdout empty** — contradicts `ejabberdctl help change_password`'s own example, which shows a printed `'ok'` on success; this ejabberd version (24.12-4) prints nothing | Exit `1`, stdout `{not_found,"unknown_user"}` — a raw Erlang tuple literal, not a sentence (nonexistent user) |
| `ban_account user host reason` | Exit `0`, empty output | (not tried — a second `ban_account` on an already-banned account wasn't exercised) |
| `unban_account user host` | Exit `0`, empty output | (not tried) |
| `unregister user host` | Exit `0`, empty output | **Exit `0`, empty output even for a user that was never registered** — `unregister` is silently idempotent, not an error, on a nonexistent user. A caller can't distinguish "removed" from "was never there" from this alone. |

**A real correction to the Design section's earlier characterization of `ban_account`:**
banning does not simply "swap in a random password" (this doc's own earlier, unverified
paraphrase of general ejabberd documentation). Verified live: a banned account enters a
distinct `account-disabled` auth state carrying the ban reason as a message. Concretely,
calling `check_password` against a banned account doesn't return a clean `false` — it throws
an **unhandled Erlang exception** in ejabberd's own CLI (`ejabberd_auth:check_password/6`
failing to match `{false,'account-disabled', <<"Account is banned: ...">>}` against its
expected cases), a large stack trace, exit `1`. **`check_password` must never be used to detect
ban status.** The safe, purpose-built command for this is `get_ban_details user host`: empty
output (exit `0`) when not banned; tab-separated `key\tvalue` lines (`reason`, `bandate`,
`lastdate`, `lastreason`) when banned. `unban_account` was confirmed to fully restore the
account's *original* password (not issue a new one) — genuinely reversible, just via a
different mechanism than assumed.
- `pyobs-web-admin` has exactly one admin identity (`ADMIN_USERNAME`/`ADMIN_PASSWORD_HASH`) —
  no per-user roles or permissions anywhere in this app today (see Design's "Who can trigger"
  decision).
- No existing "destructive action" confirmation pattern anywhere in this app to reuse —
  `stop_module`/`deactivate_module`/etc. all execute immediately on click, no confirm dialog.
  Whatever this doc lands on (see Design's Confirmation UX decision) is the first of its kind
  here, not a copy of an established convention.
- **A real fact this doc's config-write-back decision depends on:** `EJABBERD_INTEGRATION.md`'s
  own "third bug" documents that two modules in this exact fleet already share one `comm.user`
  (`_test` and `camera` both resolve to `"camera"`) — not a hypothetical edge case, a confirmed
  real configuration. Any action that changes or removes an account has to account for this.
  Confirmed further: on this box, `comm.password:` is set per-module (`telescope.yaml`: `comm:
  {<<: *comm, user: telescope, password: pyobs}`), not in the shared fragment
  (`comm.shared.yaml` only holds `class`/`domain`/`use_tls`/`ignore_cert_errors`) — so the
  common case has one password location per module, but the shared-fragment case (see Design)
  still has to be handled, not just assumed away.
- `services.save_local_acl` already has the exact precedent this doc's config-write-back needs:
  splices just the changed block into the raw config text (`_replace_local_acl_block`) rather
  than a full YAML round-trip, since the raw file can contain bare `{include ...}` lines a
  generic parser can't load; refuses to write if the block's source resolves to a shared
  fragment (`get_resolved_acl`'s `source`), raising `ValueError` naming that fragment; verifies
  the result post-write and rolls back to the original content if it doesn't match what was
  requested. `get_comm_user` currently does *not* track provenance the same way (`"No
  provenance tracking ... there's no editing use case for comm.user, only display"` — written
  before this doc existed) — this doc's write-back adds exactly that editing use case, so
  `get_comm_user`'s resolution needs to gain the same `source` tracking `get_resolved_acl`
  already has, not just get a new sibling function bolted on.

## Design

### Command scope — settled: full set

`register`, `change_password`, `ban_account`, `unban_account`, and `unregister` all ship, plus
`get_ban_details` (read-only) to display ban status — required, not optional, since "Verified
live" above showed `check_password` can't safely be used to detect a ban (it throws an
unhandled exception on the CLI rather than a clean answer). Reversible actions
(register/change_password/ban/unban) are the default-safe core; permanent `unregister` ships
too, but gated by the stronger confirmation below precisely because it's the one action here
with no undo.

### Confirmation UX — settled: tiered by risk

- `register` / `change_password` / `ban_account` / `unban_account`: a single confirm dialog
  (this app's first destructive-action confirmation of any kind, so it sets the baseline
  pattern for anything after it).
- `unregister`: retype-to-confirm (type the account's bare XMPP username, not the module name
  — see the shared-identity point below for why the username is the more correct thing to
  confirm against) before the action fires, given it's the one irreversible action in the set.

### Transport — settled: `ejabberdctl` only, no `mod_http_api` ACL change

New write functions in `modules/ejabberd.py` always go through the `ejabberdctl` subprocess
path, never `mod_http_api` — unlike the read path, where the ~50–60x latency gap decisively
favored HTTP, a write's cost here is dominated by the human clicking a confirmation dialog, not
command latency. This also means **no ejabberd-side `api_permissions` change is needed at all**
(see Current state) — a real simplification over the original `mod_http_api` setup, and one
fewer deploy-time step for this feature than `EJABBERD_INTEGRATION.md` needed.

### Config write-back — settled: yes, automatic — and it has to handle shared `comm.user`

`change_password` writes the new password back into the module's own YAML (`comm.password:`)
automatically, using the exact mechanism `save_local_acl`/`_replace_local_acl_block` already
established (splice the raw text, don't round-trip the whole file; see Current state). Refuses
the same way `save_local_acl` does if the resolved source is a shared fragment, naming it in the
error rather than silently editing something that would change every module including it.

**The shared-`comm.user` case this doc's own research surfaced (see Current state) applies to
every write action here, not just password changes** — `register`/`change_password`/
`ban_account`/`unban_account`/`unregister` on a `comm.user` shared by more than one module
affects *all* of them, not just whichever module's page the action was triggered from. Before
executing any write, resolve **every** module whose `comm.user` matches the target username
(reusing `get_comm_user` across `list_modules()`, the same way `EJABBERD_INTEGRATION.md`'s own
dashboard tile already has to reason about shared identities) and:

- `change_password`: write the new password into **every** matching module's config, not just
  the one the action was triggered from — otherwise the others are left with a silently stale
  password and break on their next (re)connect, which is a worse outcome than the feature not
  existing at all.
- `unregister` / `ban_account`: if more than one module shares the identity, say so explicitly
  in the confirmation dialog ("this account is also used by: camera, _test") before proceeding
  — the action still goes through on confirm (this app already has real modules sharing an
  identity on purpose, e.g. a `_test` copy reusing a real module's identity, so blocking outright
  would be wrong), but the operator must not be able to click through without seeing that.

### Where it surfaces

The module detail page's existing ejabberd block (Overview tab, `EJABBERD_INTEGRATION.md`'s
Work Plan item 7) is the natural home — a "Register" action when `check_account` is false,
"Reset password" / "Ban" / "Unregister" when true. No new page needed.

### Who can trigger this — settled: any logged-in admin

`pyobs-web-admin` has exactly one admin identity; there's no role system to scope this
narrower than "anyone who can log into this instance," and building one just for this feature
is out of scope. The tiered confirmation above is the safety net, not access control.

### Modules with no `comm:` block — settled: out of scope

This feature manages the account for a `comm.user` a module's config *already* declares. It
does not create a new `comm:` block for a module that has none (e.g. `HttpFileCache`) — that
would mean writing a new config section, not just a password, which is a different and larger
scope than "manage an existing identity." Matches `EJABBERD_INTEGRATION.md`'s own read-only
doc, which never writes config either.

### Hub-mode delegation

Same shape as the read path (`EJABBERD_HOST` resolution, `proxy.call()` to whichever host
actually owns `EJABBERD_API_URL` — though writes don't use `EJABBERD_API_URL` at all per the
Transport decision, the *host resolution* is identical). A write crossing the existing
hub-token-authenticated proxy isn't a new trust boundary: that channel already carries control
actions (start/stop/restart) under the same `HUB_TOKEN` gate.

## Open questions

None currently — all six original questions are settled above, and the one new question this
doc's own research surfaced (shared `comm.user` across modules) is settled too, not left open.
Revisit this section if implementation surfaces something the design didn't anticipate.

## Progress log

- **Done.** Live-verified every write command's response shape via `ejabberdctl` against a
  disposable test account — see Current state's "Verified live" table for the full findings
  (empty-stdout-on-success, `unregister`'s idempotency, `check_password`'s crash on a banned
  account, and the stdout-vs-stderr correction).
- **Done.** `get_resolved_comm(name) -> (comm_user, comm_password, source)` replaces the
  earlier no-provenance `get_comm_user` internals — `get_comm_user` is now a thin wrapper
  around it. Returning the password too (not just user/source) turned out to matter: it's
  what `register` uses to create the account with the password the module's config *already*
  declares, rather than prompting for a new one (see Design, "Command scope" — an addition to
  the original plan, decided during implementation, not before). Tests:
  new methods added to the existing `GetCommUserTests` in `modules/tests.py`.
- **Done.** `modules/ejabberd.py`: `register`/`change_password`/`ban_account`/
  `unban_account`/`unregister`/`get_ban_details`, all `ejabberdctl`-only, matching the
  verified shapes exactly (raising `ValueError` with ejabberd's own message on failure).
  Tests: `EjabberdWriteCommandTests`, fixtures are the real captured stdout/returncode pairs.
- **Done.** `services.find_modules_sharing_comm_user` + `services.save_comm_password`: the
  all-or-nothing, splice-not-round-trip, refuse-on-shared-fragment, rollback-on-partial-
  failure config write-back described in Design. `_replace_local_acl_block`'s block-locator
  was generalized into `_block_source_file(raw, key)` (was `_acl_source_file`, `acl`-only) so
  `comm:` could reuse the exact same `{include}`-detection logic acl: already had. Tests:
  `SaveCommPasswordTests`. Verified beyond unit tests, against an isolated **copy** of this
  box's real `/opt/pyobs/config` (never the live files): confirmed the `<<: *comm` anchor
  merge key survives the splice, only the two modules actually sharing an identity
  (`camera`/`_test`) get updated, and `telescope` is untouched.
- **Done.** Module-scoped write endpoints (`register`/`change-password`/`ban`/`unban`/
  `unregister` under `/api/modules/<name>/ejabberd/`) plus hub-facing "dumb" delegation
  targets (`/api/ejabberd/user/<user>/...`), mirroring `api_module_ejabberd`/
  `api_ejabberd_user`'s existing two-layer shape exactly. `api_module_ejabberd` itself
  changed: `registered`/`ban_details` are now queried and returned regardless of
  `module_running` (only `sessions`/`last` stay gated on it) — registering/resetting/banning
  an account for a module that isn't running yet is a real, intended use case, not something
  that should require starting the module first. Also gained `shared_with` (every *other*
  local module resolving to the same `comm.user`), feeding the confirmation UI's warning.
- **Done.** Tiered confirmation modal in the module detail page's ejabberd block: simple
  confirm dialog for register/change-password/ban/unban, retype-the-username for
  `unregister`. A `shared_with` warning banner appears in the modal for ban/unregister
  whenever another module shares the identity. `node --check` against the actual served
  script confirmed clean syntax.
- **Verified live, full round trip** — a scratch module config (never `/opt/pyobs/config`)
  pointed at a disposable test account, against the real ejabberd instance: register (using
  the config's own password) → `GET .../ejabberd/` showed `registered: true` → change-password
  (confirmed the new config password byte-for-byte matches what's now registered, via
  `check_password`, without ever printing the credential) → ban (`ban_details` populated with
  the default reason) → unban (`ban_details` back to `null`) → added a second module sharing
  the identity and confirmed `shared_with` reflected it → unregister → confirmed both the API
  response and `ejabberdctl registered_users` show it fully gone. Also probed the error path:
  registering a module with no `comm.password:` configured returns a clean 400, and — checked
  directly — never even reaches ejabberd (no phantom account created). All scratch fixtures
  (config dir, settings module, `sudo -n ejabberdctl` wrapper, cookies) removed afterward;
  `ejabberdctl registered_users` confirmed back to exactly the original six real accounts.

## Work Plan

- [x] Verify live, against the real instance, the actual **text** shapes `register`/
  `unregister`/`change_password`/`ban_account`/`unban_account` return via `ejabberdctl` — done
  using a disposable test account (`docverifytest99`) created and fully removed afterward,
  never a real module identity; see Current state's "Verified live" table. Found: `unregister`
  is silently idempotent on a nonexistent user (no error to catch), `change_password` prints
  nothing on success despite its own help text's example, and `check_password` must never be
  used to detect a ban (unhandled exception) — `get_ban_details` is the safe alternative.
- [x] `get_comm_user`: add `source` tracking (mirroring `get_resolved_acl`), since this doc's
  config write-back is the first editing use case for `comm.user`/`comm.password`. →
  `get_resolved_comm`, see Progress log.
- [x] `modules/ejabberd.py`: new write functions (`register`/`change_password`/`ban_account`/
  `unban_account`/`unregister`/`get_ban_details`), `ejabberdctl`-only, matching the verified
  shapes above — in particular, `register`'s and `change_password`'s failure paths need to
  raise on nonexistent-user/conflict rather than assume success from a zero-content stdout, and
  `unregister`'s success path can't assume "did this actually exist beforehand" without a
  `check_account` call first, since ejabberd itself won't tell you.
- [x] `services.py`: shared-`comm.user` lookup (which other modules resolve to the same
  username) and the config write-back (mirroring `save_local_acl`'s splice-refuse-verify
  shape), covering the multi-module case above.
- [x] New view(s)/endpoint(s) plus the tiered confirmation UI in the module detail page's
  ejabberd block.
- [x] Hub-mode delegation for the write actions, mirroring the existing read delegation
  (`_ejabberd_host_config`/proxy pattern) — code-complete and structurally identical to the
  already-verified read path, but not itself driven end-to-end against a real two-instance
  hub/spoke pair the way the read path was in `EJABBERD_INTEGRATION.md`. Worth doing before
  fully trusting this in a real multi-host fleet.
- [ ] `README.md`: document once implemented and verified live, not before — matches this
  repo's existing practice of not documenting a setting/feature before it's actually consumed
  (see `EJABBERD_INTEGRATION.md`'s Progress log, and `JOURNALD_LOGS.md`'s Work Plan, for the
  same reasoning applied there).
