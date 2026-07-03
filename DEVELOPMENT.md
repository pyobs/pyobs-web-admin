# pyobs-web-admin: ACL matrix page — v0.4 (2026-07-03, 12:41)

## Status

Design settled, implementation starting now. v0.2 added the groups/profiles concept; v0.3 added interface-name shorthand handling and the live-XMPP-discovery idea; v0.4 decides against XMPP discovery (custom wire protocol, would need real `pyobs-core` dependency) in favor of literal/badged shorthand display.

## Motivation

`pyobs-core` 2.0 adds per-module access control (`acl:` blocks in each module's own YAML config — see `pyobs-core`'s `DEVELOPMENT.md`, [Access Control (ACLs)](https://github.com/pyobs/pyobs-core/blob/develop/DEVELOPMENT.md#access-control-acls)). That design deliberately keeps ACL storage and enforcement per-module: a module's reachability is legible from its own config file, and the runtime check (`Module.execute()`) never depends on fleet-wide state. The trade-off, raised from the `pyobs-core` side: once a fleet has a dozen-plus modules each with their own `acl:` block, "who can reach the telescope, and with what" is scattered across a dozen files with no single place to read it back.

This is a visibility/authoring problem, not an enforcement one, and `pyobs-web-admin` is the right place to solve it — it already owns exactly this kind of fleet-wide, config-editing surface (dashboard across all modules, hub mode across multiple hosts, per-module Config tab).

## Current state (checked against this repo)

- `modules/services.py`'s `get_config(name)`/`save_config(name, content)` and `get_shared_config`/`save_shared_config` treat config files as **opaque text** — read and written as raw strings, no YAML parsing anywhere in this repo. `pyproject.toml` has no `pyyaml` (or any YAML library) dependency.
- `*.shared.yaml` fragments (`services.list_shared_configs`, matched via `*.shared.yaml` glob) and `{include}` references are a first-class *editing* concept (their own sidebar section, own editor) but `{include}` resolution itself doesn't happen server-side — the Config tab's "included shared configs are shown as clickable links" is a display affordance over the raw text, not an actual merge.
- `pyobs-core`'s real `{include}` resolution lives in `pyobs.utils.config.pre_process_yaml` (`pyobs-core/pyobs/utils/config.py`) — regex-driven, recursive, handles nested `{include file key}` and YAML anchors/aliases across included files. It depends only on `os`, `re`, `yaml`, `io.StringIO`, `typing` — nothing else from `pyobs-core`. That matters here because this repo's README explicitly advertises "No pyobs-core dependency — communicates with pyobs directly via subprocess," so reproducing this logic has to either vendor that one function or take a very narrow dependency, not pull in all of `pyobs-core`.
- `list_modules()` enumerates module names from config filenames in `PYOBS_CONFIG_DIR` (`*.yaml`, excluding `*.shared.yaml`). This is the existing source of truth for "which modules does this installation manage," reused below.

## Design

### What the matrix shows

One page, one table: rows = target modules (from `list_modules()`), columns = **callers**, cells = what that caller may do on that target.

**Callers are not the same set as `list_modules()`.** An `acl:` block's `allow`/`deny` entries are just caller-name strings — they don't have to correspond to a module this installation runs or even manages (a caller could be `pyobs-gui`'s or `pyobs-web-client`'s own connecting identity, an ad hoc script's JID, or a module living on a different host under hub mode). The column set is the **union of every caller name that appears in any module's resolved `acl:` block**, harvested by scanning all modules — not assumed equal to the row set. A caller name that matches a known module gets linked to that module's own page; one that doesn't is still shown, just without a link.

Cell values, derived per (target, caller) pair from the target's resolved `acl:`:

| Target's `acl:` | Cell |
|---|---|
| no `acl:` key | **open** — every caller, including ones with no row/column presence elsewhere |
| `allow: {caller: "*"}` | **all methods** |
| `allow: {caller: [m1, m2]}` | **m1, m2** |
| `allow: {...}`, caller not listed | **denied** |
| `deny: [caller, ...]`, caller listed | **denied** |
| `deny: [...]`, caller not listed | **all methods** |
| any of the above with `mode: log` | same computed value, visually flagged (e.g. a badge) as **not yet enforced** |

"Open" targets (no `acl:` at all) are worth surfacing prominently rather than just leaving blank — the matrix's main value is spotting modules that *should* have a policy and don't, not just displaying ones that already do.

> **Call-out:** an `allow` entry may itself be an interface name (e.g. `ICamera`) rather than a method name — `pyobs-core` expands this at runtime into that interface's full method list (see below). The matrix does **not** perform this expansion; it shows the entry as-is, badged to distinguish it from a plain method name (e.g. `ICamera (interface)`). See "Interface-name shorthand" below for why, and for a possible way to close this gap later.

### Resolving `{include}` correctly

The matrix has to read each module's **effective** config, not its literal file content — an `acl:` block that lives in a `*.shared.yaml` fragment and is pulled in via `{include acl.shared.yaml}` must show up for every module that includes it. Vendor `pre_process_yaml` (or a trimmed subset covering just `{include}`, if the anchor/alias handling turns out unnecessary for `acl:` blocks specifically) rather than reimplementing `{include}` parsing independently — two independent implementations of the same include syntax drifting apart is a worse outcome than one vendored copy with a comment noting its origin (`pyobs-core/pyobs/utils/config.py`) and a `pyobs-core` version it was last synced against, re-checked whenever `pyobs-core`'s version bumps.

Add `pyyaml` as a real dependency of this repo — required either way (vendoring `pre_process_yaml` still needs it), and matches what `pyobs-core` already depends on.

### Interface-name shorthand: static display vs. live XMPP discovery

`Module.__init__` (`pyobs-core/pyobs/modules/module.py`) expands interface names in `allow` entries into that interface's full method list via `_get_interfaces_and_methods()`, which does `isinstance(self, interface)` against the module's **actual runtime class** — a class that can live in `pyobs-core` itself or in any device-driver package (`pyobs-sbig`, `pyobs-fli`, `pyobs-alpaca`, ...). Reproducing that expansion statically would mean importing the concrete class named in every module's `class:` key, i.e. potentially every device package present in the fleet, installed into `pyobs-web-admin`'s own venv — a far bigger dependency footprint than the one vendored `{include}` function, and it reintroduces the coupling the "no `pyobs-core` dependency" design principle exists to avoid. First cut, therefore: show interface-name shorthand as a literal, badged cell value rather than expanding it (see call-out above) — the matrix's job is to surface configured policy, not simulate `Module.execute()`'s runtime resolution bit-for-bit.

**Idea raised: a dedicated XMPP account for `pyobs-web-admin`.** `Comm.get_interfaces(client)` (`pyobs-core/pyobs/comm/comm.py`, implemented in `pyobs/comm/xmpp/xmppcomm.py` over `slixmpp`) asks a *running* module, live, which interfaces it implements — this is XMPP disco-based, resolved by the module itself (which already has its own class and `pyobs-core` loaded) and returned as interface **names** over the wire, so the querying side never needs to import any device-driver class at all. Paired with a small vendored, `pyobs-core`-only static table of interface name → method names (built from `pyobs.interfaces`, which unlike device drivers is small, stable, and has no hardware coupling), this would let the matrix expand interface-name shorthand into real method lists without ever importing a device package — closing the gap the static approach above can't.

**Decided: not pursuing this.** `get_interfaces` isn't standard XEP-0030 disco — it rides on `pyobs-core`'s own bespoke stanzas (`pyobs:event`, `pyobs:state`) and a custom `rpc.py`/`serializer.py` protocol for dataclasses and interface schemas. A "thin, vendored XMPP client" would mean reimplementing that custom wire protocol too, which is a much bigger and more drift-prone undertaking than the one `pre_process_yaml` function — doing this properly means using `pyobs-core`'s actual `XmppComm`, i.e. taking the real dependency, not a narrow one. That also adds a live network dependency (XMPP server reachability, credentials, an async session) to what is otherwise a fast, file-based page, and only works for modules that happen to be running at query time. Weighed against what it buys — expanding a cosmetic shorthand into a method list — that's disproportionate, and it breaks the README's explicit "no `pyobs-core` dependency" claim for a non-essential feature. Staying with the narrow vendor approach and literal/badged shorthand display (see call-out and cell-rendering item above) for the foreseeable future; revisit only if some other feature creates independent justification for a full `pyobs-core` dependency.

### Editing from the matrix

A cell edit has to land in the file the rule actually came from, which is not always the target module's own file:

- If the target's `acl:` block is **not** behind an `{include}`, edit and save directly via the existing `save_config` path — this is the common case and needs no new semantics.
- If the target's `acl:` block **is** pulled in from a shared fragment, editing it in place would silently change every other module that includes the same fragment. The matrix must show this ("this rule comes from `acl.shared.yaml`, included by 4 modules") and either open the shared fragment's own editor (existing `shared_detail` view) for the edit, or require an explicit "detach into this module's own config" action before allowing an inline edit — not silently write through to a file whose blast radius is bigger than the one row being edited.

### Groups (a.k.a. profiles)

`acl:` entries are just caller-name strings, and the same clusters of callers tend to recur across many modules' `allow`/`deny` lists (e.g. "the set of ops scripts," "the GUI's identities"). Editing each occurrence by hand doesn't scale past a handful of modules, but this abstraction has to live entirely in `pyobs-web-admin` — `pyobs-core` and the config file format have no concept of a group and never should, since that would reintroduce the fleet-wide state the underlying ACL design deliberately avoids.

**Definition and storage.** A group is a name mapped to a list of caller identities, defined and stored only in `pyobs-web-admin` — not in `PYOBS_CONFIG_DIR`, not in any `*.yaml` or `*.shared.yaml` file. This repo has no database today; a group store is new persisted state, likely the smallest thing that works (a JSON/YAML file under app-local storage) rather than pulling in a full DB for this alone. Exact storage mechanism is deferred to implementation.

**Expansion is one-shot, not a live binding.** Assigning a group to a target's `allow`/`deny` entry expands it into a literal caller list at save time, written into the module's own config (or shared fragment, per the existing editing rules above). The file on disk always contains plain caller strings — no group reference — so it stays legible to `pyobs-core` and to anyone reading it by hand, matching the doc's existing principle that the config file is the source of truth. Consequently, editing a group's membership later does **not** retroactively rewrite files that already used it. `pyobs-web-admin` should record, per expanded rule, which group and which membership snapshot produced it, and offer an explicit **"re-apply group"** action on affected rules — the same shape as the shared-fragment "detach" action already described above, not automatic propagation.

**Drift detection, not recovery.** Since ACL edits are assumed to happen only through `pyobs-web-admin`, it can record a hash of each `acl:` block's content at the time it last wrote it. If a later load finds the live file's hash doesn't match, warn the admin that the block changed outside `pyobs-web-admin` (so any tracked group-provenance for that rule may be stale) — no attempt to auto-reconcile. Reconstructing "which parts of a hand-edited flat list came from which group" isn't well-defined, so the right move is to surface the drift and let the admin decide whether to keep the manual edit or re-apply a group, not to guess a resolution.

**Recovering candidate groups from existing configs.** Since real fleets will already have `acl:` blocks with no group concept behind them, offer a one-time (or on-demand) "suggest groups" pass over the matrix data: for each target, bucket its callers by identical permission value (same method list, or both granted `"*"`), then across all targets look for caller-sets that recur identically in two or more targets. Each recurring set is shown as an unnamed candidate ("these 3 callers grant identical permissions in 4 targets — name this group?") for the admin to confirm and name; nothing is created or rewritten automatically. Start with exact-set matching only, not subset/fuzzy matching — real groups will have one-off exceptions (an extra method granted to one member, a module that additionally denies one) that exact matching will miss, but loosening the match risks suggesting bogus groups on fleets with few targets, which is the worse failure mode for a tool whose only job is to propose candidates a human still has to confirm.

### Hub mode interaction

Hub mode already proxies dashboard/config/log actions to remote hosts transparently. The matrix should do the same — aggregate across every configured host, not just the local one — since ACL policy for a real multi-host fleet is exactly the kind of thing that's easy to get wrong on one host and forget on another. This falls out of reusing the existing hub-proxying mechanism rather than needing new cross-host plumbing, but is worth calling out explicitly as a requirement, not an incidental nice-to-have.

## Open questions

- Exact UI treatment for "open" targets and `mode: log` rules (color/badge choice) — a UI/visual-design decision, not an architectural one, deferred to implementation.
- Whether to offer the "detach from shared fragment into this module's own config" action as a one-click automatic rewrite, or just point the admin at the shared fragment's editor and let them decide by hand. Leaning toward the latter for a first version — automatically rewriting a shared `{include}` into a module-local override is a bigger, riskier piece of config surgery than this feature needs to solve on day one.
- Exact storage mechanism for the group store (JSON/YAML file vs. a small DB table) — deferred, not architecturally significant either way as long as it stays local to `pyobs-web-admin`.
- Whether "suggest groups" is a one-time first-run action or something re-run on demand as configs evolve — leaning toward on-demand, since new recurring caller-sets can appear at any point as modules are added.

## Work Plan

- [ ] Add `pyyaml` to `pyproject.toml`.
- [ ] Vendor (or reimplement, scoped to just `{include}`) `pre_process_yaml`-equivalent resolution; unit-test against `pyobs-core`'s own test fixtures for `{include}` if available, to catch drift early.
- [ ] `services.py`: a function that, for a given module name, returns its resolved `acl:` block (`dict | None`) plus, if present, which file it actually came from (the module's own config, or a named shared fragment).
- [ ] `services.py`: a fleet-wide scan building the (target × caller) matrix data structure described above, including the "callers not in `list_modules()`" case.
- [ ] Cell rendering: show interface-name shorthand entries (e.g. `ICamera`) as a literal, badged value, not expanded into method names.
- [ ] New view + template + URL entry (`modules/urls.py`) for the matrix page, following the existing dashboard/module_detail pattern.
- [ ] Editing: direct save for module-local `acl:` blocks; shared-fragment case routes to (or at minimum clearly links to) the existing `shared_detail` editor rather than writing through silently.
- [ ] Hub mode: confirm the matrix aggregates across configured remote hosts using the existing proxying mechanism.
- [ ] Groups: local store for name → caller-list definitions.
- [ ] Groups: expand-on-save into literal `allow`/`deny` entries; record per-rule group + membership snapshot for later re-apply.
- [ ] Groups: per-`acl:`-block content hash, checked on load; surface a warning (no auto-recovery) when it doesn't match the last write.
- [ ] Groups: "suggest groups" pass — bucket callers by identical permission value per target, find exact-match recurring sets across targets, present as unnamed candidates for the admin to confirm/name.
