# specs/

- **`specs/design/`** — living design docs, one per feature (see `design/README.md` for the
  index and the per-doc shape). `specs/plans/` (implementation plans, checklist-style) doesn't
  exist yet here — add it if an unimplemented feature needs one; a plan moves/folds into
  `design/` once it ships.

These are `pyobs-web-admin`-local docs. `pyobs-core` is the reference for the full `specs/`
convention across the `pyobs` ecosystem (it additionally has `adrs/` and `steering/`,
and its own `CLAUDE.md` explains when a doc belongs there instead of here — e.g. anything
that also concerns another sibling repo). See `pyobs-core`'s `specs/` for cross-repo design
docs, plans, and ADRs that happen to touch this repo (tagged with a `Repos:` line there).
