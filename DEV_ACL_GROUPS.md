# pyobs-web-admin: ACL Groups (a.k.a. profiles) — reverted, kept for reference

## Status

**Implemented, then reverted at explicit request.** This was built out fully across three
commits — a JSON storage layer (`PYOBS_STORAGE_DIR`), a standalone `/groups/` management
page, and expand-on-save wiring into both ACL editing surfaces (the matrix's per-row modal
and `module_detail`'s ACL tab) — and then reverted (`git revert`, not a history rewrite,
since the commits were already pushed) after using it. No specific reason was given beyond
not liking it; this doc exists so the design work and the implementation experience aren't
lost if Groups comes up again later. `DEV_ACL_MATRIX.md` itself has no remaining trace of this
feature — Work Plan items 9–12 are back to their original "Deferred — later" wording, and
this doc is the complete record instead.

## Motivation

`acl:` entries are just caller-name strings, and the same clusters of callers tend to recur
across many modules' `allow`/`deny` lists (e.g. "the set of ops scripts," "the GUI's
identities"). Editing each occurrence by hand doesn't scale past a handful of modules, but
this abstraction has to live entirely in `pyobs-web-admin` — `pyobs-core` and the config file
format have no concept of a group and never should, since that would reintroduce the
fleet-wide state the underlying `acl:` design deliberately avoids.

**Real production use case, raised in discussion:** a typical fleet has three caller
categories. The core telescope/camera/robotic-control modules should be able to call each
other without restriction. Admins connecting via `pyobs-gui` also need full access. Students
and guests should only reach a pre-defined subset of modules. All three map cleanly onto
groups of *callers* (not targets) — "students can only reach modules A, B, C" is expressed by
adding a `students` group to the allow list of A, B, and C specifically, not by defining a
group of targets; "which modules can students reach" falls out of reading a column in the
existing ACL matrix, it doesn't need its own concept.

## Design

### Groups are caller-groups, not target-groups

Confirmed directly against the use case above: `pyobs`'s `acl:` block always lives on the
*target* module, declaring who may call it. A group is therefore always a named list of
*callers*, reused across however many targets' `allow`/`deny` lists need it — there is no
symmetric "group of targets" concept, and none was needed; the ACL matrix's own caller
columns already answer "which targets can group X reach" without anything new.

### Definition and storage

A group is a name mapped to a list of caller identities, defined and stored only in
`pyobs-web-admin` — not in `PYOBS_CONFIG_DIR`, not in any `*.yaml` or `*.shared.yaml` file.
This repo has no database and didn't need one for this: a group store is just a flat
`name -> list of caller strings` mapping with no queries beyond "look up by name."

**Settled: JSON file under a new `PYOBS_STORAGE_DIR` setting**, alongside
`PYOBS_CONFIG_DIR`/`PYOBS_LOG_DIR`/`PYOBS_RUN_DIR` (default `/opt/pyobs/storage`, matching
that existing `/opt/pyobs/<subdir>` naming convention). Not `PYOBS_RUN_DIR`, even though it
already exists — its actual semantics are ephemeral per-process state (PID files that come
and go with module lifecycle), a different category from a group store's persistent app
state. Not `PYOBS_CONFIG_DIR` either, since `pyobs-core` never reads this file or knows
groups exist, unlike every other file that directory holds — this would have been the first
file in the project that's purely `pyobs-web-admin`'s own bookkeeping, not a `pyobs` config
artifact. Written atomically (temp file in the same directory + `os.replace`), unlike a
per-module config write — a crash mid-write to a *single* group's config risks one file,
whereas one JSON blob holding *every* group risks losing all of them at once.

### Alternate storage option: ejabberd shared-roster groups — considered, not used

Raised after `DEV_EJABBERD_INTEGRATION.md` shipped: ejabberd's `mod_shared_roster` has its own
group concept — a name mapped to a list of JIDs, queryable via the same HTTP API
(`srg_list`/`srg_get_info`/`srg_get_members`-family commands) `modules/ejabberd.py` already
wraps for other things. Since the caller-identity space is already shared 1:1 with XMPP JID
local-parts, a shared-roster group's member list would be directly usable as a group's caller
list, with no separate identity mapping needed.

This would only have replaced the storage/definition half, not the expansion model —
membership would be fetched live from ejabberd at the moment an admin picks a group to
assign, but assignment would still expand to a literal caller list at save time (`pyobs-core`
still never talks to ejabberd or knows what a group is either way). Tradeoffs against the
local-file option: (a) requires `EJABBERD_ENABLED` at all, so it can never be the *only*
backend; (b) shared rosters are a presence/visibility feature, not an authorization concept —
repurposing them means whoever manages the XMPP server can silently reshape ACL groups as a
side effect of unrelated roster administration, a messier blast radius than a file only this
app writes; (c) would genuinely avoid defining the same group twice in two places, *if* a
fleet's shared rosters were already organized by role.

**Confirmed directly, not just theorized: this doesn't apply here.** Student/guest accounts
are not already organized into an ejabberd shared roster for any other reason (provisioning,
presence, or otherwise) — setting one up would be new work done purely for this feature, not
reuse of something that already exists. That removes the one thing (c) that would have made
this worth the tradeoffs in (a)/(b), so the local-file store was the only option actually
built.

### Known-caller identity: module names, not resolved `comm.user`/JIDs

The Groups UI needed a "pick from known possible members" list. Considered resolving each
module's actual `comm.user` (its real XMPP identity) instead of its module/file name, since
technically the ACL-relevant identity *is* the JID local-part, and a module's `comm.user` can
in principle differ from its own module name. Rejected for consistency, not correctness: this
whole feature area already treats module names as the caller-identity proxy throughout — the
ACL matrix's own caller columns and `module_detail`'s ACL tab checkboxes both already work
this way, and `DEV_ACL_MATRIX.md`'s own Design section is explicit that "callers are just
caller-name strings," not something requiring XMPP resolution. Introducing JID resolution
just for Groups would have been a new, inconsistent identity model bolted onto one corner of
a feature that already has an established one everywhere else.

The "known callers" list that was built reused `build_acl_matrix()`'s own `callers` set —
every module name, plus every caller string already typed somewhere in an `acl:` block (e.g.
`scheduler`) — exactly the same identity space the rest of the ACL feature already exposes,
not a new concept.

### Expansion is one-shot, not a live binding

Assigning a group to a target's `allow`/`deny` entry expands it into a literal caller list at
save time, written into the module's own config (or shared fragment, per the existing editing
rules). The file on disk always contains plain caller strings — no group reference — so it
stays legible to `pyobs-core` and to anyone reading it by hand, matching the ACL matrix's own
existing principle that the config file is the source of truth. Consequently, editing a
group's membership later does **not** retroactively rewrite files that already used it.

**What was actually built:** both ACL editing surfaces (the matrix's per-row modal and
`module_detail`'s ACL tab) gained a group `<select>` + "Add group" button. Selecting a group
expanded its members into the existing checkbox (for a caller already known to that page,
checked and set to "all methods") or a new freeform row (for anything else — including a
target module's own name, which is deliberately absent from its own "known modules" list, or
a human-only account) — with duplicate-avoidance so re-adding an overlapping group never
created duplicate rows. `services.record_group_application(module_name, group_name,
snapshot)` persisted, per module, which group (and what its exact membership was) produced
which save, in a second JSON file (`acl_group_applications.json`) alongside the groups file
itself, atomically written the same way. `views.api_acl`'s POST accepted an
`applied_groups` field and recorded it *after* `save_local_acl` succeeded — recording was
pure bookkeeping, it never blocked or altered the save itself, and it was forwarded through
unchanged when proxying to a remote host so the recording always happened on whichever host
actually owned the module's config.

**Re-apply (reading that recorded snapshot back to show drift, and offering a resync) was
never built** — see "Drift detection" and the Work Plan below. The recording mechanism
existed only to make that future action possible, not to do anything with it yet.

### Drift detection, not recovery

Since ACL edits are assumed to happen only through `pyobs-web-admin`, it could record a hash
of each `acl:` block's content at the time it last wrote it. If a later load finds the live
file's hash doesn't match, warn the admin that the block changed outside `pyobs-web-admin`
(so any tracked group-provenance for that rule may be stale) — no attempt to auto-reconcile.
Reconstructing "which parts of a hand-edited flat list came from which group" isn't
well-defined, so the right move would have been to surface the drift and let the admin decide
whether to keep the manual edit or re-apply a group, not to guess a resolution. **Not
started** — this was Work Plan item 11, never reached.

### Recovering candidate groups from existing configs

Since real fleets already have `acl:` blocks with no group concept behind them, a one-time
(or on-demand) "suggest groups" pass over the matrix data was proposed: for each target,
bucket its callers by identical permission value (same method list, or both granted `"*"`),
then across all targets look for caller-sets that recur identically in two or more targets.
Each recurring set would show as an unnamed candidate ("these 3 callers grant identical
permissions in 4 targets — name this group?") for the admin to confirm and name; nothing
created or rewritten automatically. Exact-set matching only, not subset/fuzzy matching — real
groups will have one-off exceptions that exact matching would miss, but loosening the match
risks suggesting bogus groups on fleets with few targets, the worse failure mode for a tool
whose only job is proposing candidates a human still has to confirm. **Not started** — Work
Plan item 12, never reached.

## Open questions

- Whether "suggest groups" (if ever built) is a one-time first-run action or something
  re-run on demand as configs evolve — leaning toward on-demand, since new recurring
  caller-sets can appear at any point as modules are added.
- Whether ejabberd shared-roster groups are worth reconsidering as a *second* membership
  source later, if a fleet ever does organize its shared rosters by role — see "considered,
  not used" above. Nothing currently points at this being needed.

## Work Plan (as it stood when reverted)

- [x] Groups: local store for name → caller-list definitions. *(implemented, then reverted)*
- [ ] Groups: ejabberd shared-roster groups as an alternate, read-only membership source —
  considered and set aside, not just deferred; see "Alternate storage option" above.
- [x] Groups: expand-on-save into literal `allow`/`deny` entries; record per-rule group +
  membership snapshot for later re-apply. *(implemented, then reverted)*
- [ ] Groups: per-`acl:`-block content hash, checked on load; surface a warning (no
  auto-recovery) when it doesn't match the last write. Never started.
- [ ] Groups: "suggest groups" pass — bucket callers by identical permission value per
  target, find exact-match recurring sets across targets, present as unnamed candidates for
  the admin to confirm/name. Never started.
