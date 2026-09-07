# CLAUDE.md

Entry points for working in this repo.

## What this is

`pyobs-web-admin` is a web-based administration interface for `pyobs`: start, stop, and restart
modules, tail and filter their logs, and view/edit their configuration files, all from a browser.
No database — sessions are signed cookies (see `docs/source/architecture.rst`).

## Design history and planning

This repo keeps its own local design docs and plans: `specs/design/` (living, one per feature) and
`specs/plans/` (checklist-style; folds into `design/` once shipped) — see `specs/index.md` and
`specs/design/index.md`/`specs/plans/index.md` for the current lists. `pyobs-core`'s `specs/` is
the reference for the full fleet-wide convention (it additionally has `adrs/`/`steering/`) and
holds any cross-repo docs that happen to touch this repo, tagged with a `Repos:` line (e.g.
Keycloak-based shared authorization).

## Tooling

- Lint: `ruff` (config in `pyproject.toml`)
- Type checking: `pyrefly` (`project-includes = ["modules", "pyobs_web_admin"]`)
- Tests: `uv run python manage.py test modules` (plain `unittest.TestCase`, not Django's —
  this app has no database to wrap in transactions)
