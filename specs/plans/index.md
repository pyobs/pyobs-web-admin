# Plans

Implementation plans, checklist-style. A plan moves/folds into `design/` once it ships.

- [2026-08-07-git-separate-source-dir.md](2026-08-07-git-separate-source-dir.md) — clone git
  repos to a separate source dir with a symlink to config. **implemented, closed** (#38)
- [2026-08-15-show-running-module-versions.md](2026-08-15-show-running-module-versions.md) — show
  the pyobs-* versions each running module loaded, flag outdated ones, restart them.
  **implemented** (#51)
- [2026-08-16-async-package-update.md](2026-08-16-async-package-update.md) — run package updates
  as a background job with a live log tail instead of a blocking, timeout-prone request.
  **implemented** (#50)
- [2026-08-19-show-logs-for-config-and-comm-name.md](2026-08-19-show-logs-for-config-and-comm-name.md)
  — show a module's logs under both its config name and its comm name. **implemented** (#59)

## Not finished

(none)
