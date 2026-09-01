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
- [2026-08-19-browser-notifications-for-log-warnings.md](2026-08-19-browser-notifications-for-log-warnings.md)
  — browser notifications for log warnings and errors. **implemented, closed** (#44, PR #61)
- [2026-08-25-module-classes-fleet-aggregation.md](2026-08-25-module-classes-fleet-aggregation.md)
  — make `api_module_classes` fleet-aware instead of always-local, following the `api_all_logs`
  self-aggregating pattern. **implemented (pyobs-web-admin side); portal follow-up open** (#68,
  pyobs-portal#119)
- [2026-09-01-update-all-packages.md](2026-09-01-update-all-packages.md) — sequential "update all"
  button on the Packages page, queuing over the existing single-job update endpoint.
  **implemented, closed** (#79, PR #81)
- [2026-09-01-log-fullscreen-button.md](2026-09-01-log-fullscreen-button.md) — fullscreen toggle
  for the log console on All Logs and the per-module Logs tab, overlay-only (no native Fullscreen
  API, for iOS Safari support). **proposed** (#74)
