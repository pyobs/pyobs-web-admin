# Plans

Implementation plans, checklist-style. A plan moves/folds into `design/` once it ships.

- [2026-08-07-git-separate-source-dir.md](2026-08-07-git-separate-source-dir.md) — clone git
  repos to a separate source dir with a symlink to config. **implemented, closed** (#38)
- [2026-08-15-show-running-module-versions.md](2026-08-15-show-running-module-versions.md) — show
  the pyobs-* versions each running module loaded, flag outdated ones, restart them.
  **implemented** (#51)

## Not finished

- [2026-08-16-async-package-update.md](2026-08-16-async-package-update.md) — run package updates
  as a background job with a live log tail instead of a blocking, timeout-prone request. **planned**
