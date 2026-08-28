# specs/

- [design/](design/index.md) — living design docs, one per feature (see `design/index.md` for the
  index and the per-doc shape).
- [plans/](plans/index.md) — implementation plans, checklist-style; a plan moves/folds into
  `design/` once it ships.

These are `pyobs-web-admin`-local docs. `pyobs-core` is the reference for the full `specs/`
convention across the `pyobs` ecosystem (it additionally has `adrs/` and `steering/`, and its own
`CLAUDE.md` explains when a doc belongs there instead of here — e.g. anything that also concerns
another sibling repo). See `pyobs-core`'s `specs/` for cross-repo design docs, plans, and ADRs that
happen to touch this repo (tagged with a `Repos:` line there) — e.g.
`../../pyobs-core/specs/design/shared-authz-keycloak.md` (proposed: centralized authorization via
Keycloak groups; replaces this repo's Django-admin activation for Keycloak-linked users).
