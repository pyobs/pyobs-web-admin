# pyobs-web-admin: ACL matrix page — v0.1 (2026-07-03, 11:00)

## Status

Design only, nothing implemented yet. First entry in this file.

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

### Resolving `{include}` correctly

The matrix has to read each module's **effective** config, not its literal file content — an `acl:` block that lives in a `*.shared.yaml` fragment and is pulled in via `{include acl.shared.yaml}` must show up for every module that includes it. Vendor `pre_process_yaml` (or a trimmed subset covering just `{include}`, if the anchor/alias handling turns out unnecessary for `acl:` blocks specifically) rather than reimplementing `{include}` parsing independently — two independent implementations of the same include syntax drifting apart is a worse outcome than one vendored copy with a comment noting its origin (`pyobs-core/pyobs/utils/config.py`) and a `pyobs-core` version it was last synced against, re-checked whenever `pyobs-core`'s version bumps.

Add `pyyaml` as a real dependency of this repo — required either way (vendoring `pre_process_yaml` still needs it), and matches what `pyobs-core` already depends on.

### Editing from the matrix

A cell edit has to land in the file the rule actually came from, which is not always the target module's own file:

- If the target's `acl:` block is **not** behind an `{include}`, edit and save directly via the existing `save_config` path — this is the common case and needs no new semantics.
- If the target's `acl:` block **is** pulled in from a shared fragment, editing it in place would silently change every other module that includes the same fragment. The matrix must show this ("this rule comes from `acl.shared.yaml`, included by 4 modules") and either open the shared fragment's own editor (existing `shared_detail` view) for the edit, or require an explicit "detach into this module's own config" action before allowing an inline edit — not silently write through to a file whose blast radius is bigger than the one row being edited.

### Hub mode interaction

Hub mode already proxies dashboard/config/log actions to remote hosts transparently. The matrix should do the same — aggregate across every configured host, not just the local one — since ACL policy for a real multi-host fleet is exactly the kind of thing that's easy to get wrong on one host and forget on another. This falls out of reusing the existing hub-proxying mechanism rather than needing new cross-host plumbing, but is worth calling out explicitly as a requirement, not an incidental nice-to-have.

## Open questions

- Exact UI treatment for "open" targets and `mode: log` rules (color/badge choice) — a UI/visual-design decision, not an architectural one, deferred to implementation.
- Whether to offer the "detach from shared fragment into this module's own config" action as a one-click automatic rewrite, or just point the admin at the shared fragment's editor and let them decide by hand. Leaning toward the latter for a first version — automatically rewriting a shared `{include}` into a module-local override is a bigger, riskier piece of config surgery than this feature needs to solve on day one.

## Work Plan

- [ ] Add `pyyaml` to `pyproject.toml`.
- [ ] Vendor (or reimplement, scoped to just `{include}`) `pre_process_yaml`-equivalent resolution; unit-test against `pyobs-core`'s own test fixtures for `{include}` if available, to catch drift early.
- [ ] `services.py`: a function that, for a given module name, returns its resolved `acl:` block (`dict | None`) plus, if present, which file it actually came from (the module's own config, or a named shared fragment).
- [ ] `services.py`: a fleet-wide scan building the (target × caller) matrix data structure described above, including the "callers not in `list_modules()`" case.
- [ ] New view + template + URL entry (`modules/urls.py`) for the matrix page, following the existing dashboard/module_detail pattern.
- [ ] Editing: direct save for module-local `acl:` blocks; shared-fragment case routes to (or at minimum clearly links to) the existing `shared_detail` editor rather than writing through silently.
- [ ] Hub mode: confirm the matrix aggregates across configured remote hosts using the existing proxying mechanism.
