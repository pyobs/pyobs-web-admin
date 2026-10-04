# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries for releases before this file existed were generated from commit subjects.

## [2.3.7] - 2026-10-04

- Add MIT LICENSE

## [2.3.6] - 2026-09-29

- Use brand color (orange) as page accent color (#101)

## [2.3.5] - 2026-09-19

- Surface failed log fetches in the log windows
- Fix log viewer crash on journald byte-array fields

## [2.3.4] - 2026-09-14

- Move log-window text search to a server-side grep over full history

## [2.3.3] - 2026-09-13

- Serve favicon.ico at site root

## [2.3.2] - 2026-09-08

- Fix shared config edits leaking to the wrong hub

## [2.3.1] - 2026-09-07

- Fix log viewer crash on journal entries with a null MESSAGE field
- Add CLAUDE.md entry point pointing to specs/ conventions and tooling

## [2.3.0] - 2026-09-03

- Flag running modules as needing restart after their config file changes

## [2.2.2] - 2026-09-03

- git_pull: auto-stash uncommitted changes around the pull

## [2.2.1] - 2026-09-03

- Force-sync tags on fetch; surface proxied errors; gate Push on behind

## [2.2.0] - 2026-09-03

- fix: pause log auto-refresh rebuild during an active text selection
- docs: mark log-fullscreen-button plan implemented, closed (#74, PR #83)
- address review: aria-pressed on fullscreen toggle, update plan doc to match implementation
- feat: add fullscreen toggle for log console (#74)
- docs: mark update-all-packages plan implemented (PR #81)
- docs: add plan for log fullscreen button (#74)
- Address review: fix concurrent-run race, resumed-job button state, wording
- Add "Update all" bulk action to the Packages page
- Fix "(no matching log lines)" when a time-range end date is set
- Wire in KeycloakSessionRefreshMiddleware, require pyobs-auth>=2.1.0
- Default ENFORCE_LOCAL_ACTIVE=True in example/base config, preserving pre-2.1 is_active gating on top of the Keycloak group gate
- Fix stale sqlite/session doc lines: db.sqlite3 now also holds sessions; note clearsessions for long-lived deployments
- Address review: switch off signed-cookie sessions before storing Keycloak refresh tokens
- Centralize authorization via Keycloak groups, drop local activation gate
- Specs: reference shared-authz-keycloak design doc (#823)
- Require stable pyobs-auth>=2.0.0
- Add UI screenshots to docs and README
- Remove specs/design/ links from README
- Address review: nested-hub collisions, unreachable propagation, CI fix
- Make api_module_classes fleet-aggregating (issue #68)
- Add Dependabot auto-merge workflow
- Add CI: tests, ruff, pyrefly workflows (issue #70)
- Add missing deps to docs/requirements.txt
- Add design doc for fleet-aware api_module_classes (issue #68)
- Update pyobs-robotic-backend references to pyobs-portal
- Fix CSRF cookie name in frontend JS after cookie rename
- Give session/CSRF cookies a project-specific name
- Add GET /api/modules/classes/ for external module-class lookups (#65)

## [2.1.2] - 2026-09-01

- Fix "(no matching log lines)" when a time-range end date is set

## [2.1.0] - 2026-08-31

- Wire in KeycloakSessionRefreshMiddleware, require pyobs-auth>=2.1.0
- Default ENFORCE_LOCAL_ACTIVE=True in example/base config, preserving pre-2.1 is_active gating on top of the Keycloak group gate
- Fix stale sqlite/session doc lines: db.sqlite3 now also holds sessions; note clearsessions for long-lived deployments
- Address review: switch off signed-cookie sessions before storing Keycloak refresh tokens
- Centralize authorization via Keycloak groups, drop local activation gate
- Specs: reference shared-authz-keycloak design doc (#823)

## [2.0.0] - 2026-08-26

- Require stable pyobs-auth>=2.0.0
- Add UI screenshots to docs and README
- Remove specs/design/ links from README
- Address review: nested-hub collisions, unreachable propagation, CI fix
- Make api_module_classes fleet-aggregating (issue #68)
- Add Dependabot auto-merge workflow
- Add CI: tests, ruff, pyrefly workflows (issue #70)
- Add missing deps to docs/requirements.txt
- Add design doc for fleet-aware api_module_classes (issue #68)
- Update pyobs-robotic-backend references to pyobs-portal
- Fix CSRF cookie name in frontend JS after cookie rename
- Give session/CSRF cookies a project-specific name
- Add GET /api/modules/classes/ for external module-class lookups (#65)
- Dual login buttons: one-click IdP login via kc_idp_hint
- docs: mark browser-notifications plan implemented, closed (#44)
- Fix latest version lookup for ssh-installed git packages without a pinned ref
- Skip notify-diff when rawLogLines is empty (#44)
- Add browser notifications for log warnings and errors (#44)
- Add browser notifications plan (#44)
- Mark async package update plan implemented (#50)
- Show logs for both config name and comm name of a module (#59)
- Restart All only restarts currently running modules
- Abort merge on git pull conflict
- Pass explicit git identity on pull, not just commit
- Make Git Config page buttons more self-explanatory
- Fix git pull failing on divergent branches with no configured reconcile strategy
- Fix Git Config page showing the wrong host's repo status via the hub
- Fix stale_packages flagging every unrelated installed package as outdated
- Reference PR #51 in plans index
- Show each running module's loaded pyobs-* versions, flag outdated ones
- Make the update lock atomic and detect PID reuse after a reboot
- Run package updates as a detached background job with a live log tail
- Mark git-separate-source-dir plan implemented
- Link design/plans indexes from specs index; split unfinished plans
- Rename specs README files to index.md; add plans index
- Add running-module versions plan; date-prefix plan filenames
- Make the log time-range start date a server-side since bound
- Serve static files in production via Whitenoise
- Allow deploy.sh to install outside /opt/pyobs/pyobs-web-admin
- Move admin-account sync to post_migrate, matching archive/robotic-backend
- Add optional Keycloak SSO with per-user activate/deactivate via /admin/
- Fix swapped ahead/behind counts in git_status()
- Fix VCS package update check using stale ref after a branch switch
- Fix host-switch redirect landing on stale module pages (#46)
- Add a distinct favicon and login icon
- Add configurable pyobs logo to sidebar header
- Add light/dark mode switcher
- Upgrade uv.lock to clear open Dependabot alerts

## [1.14.4] - 2026-08-07

- fix: catch OSError on repo directory creation in git_clone(),

## [1.14.3] - 2026-08-07

- fix: use git_repo_exists() instead of hardcoding True in git_config_page,

## [1.14.2] - 2026-08-07

- fix: relax _git_config_ok for pre-symlink and legacy states

## [1.14.1] - 2026-08-07

- feat: separate source dir with symlink to config
- show declared version alongside commit hash for git-installed packages
- Reorganize dev-docs/ into specs/design/ (and fold git-backed-config plan into it)

## [1.14.0] - 2026-08-05

- show app version in sidebar; fix git push failing with unknown author identity

## [1.13.0] - 2026-08-05

- detect real update availability for git-installed packages; fix dead git push/pull buttons

## [1.12.0] - 2026-08-05

- add log level filter to log panels

## [1.11.1] - 2026-08-04

- fix color coding on fleet-wide log page

## [1.11.0] - 2026-08-03

- add unacknowledged-only filter to log panels

## [1.10.4] - 2026-07-31

- add PYOBS_CONFIG_ACL_MATRIX_ENABLED setting (off by default)

## [1.10.3] - 2026-07-31

- remember page when switching host

## [1.10.2] - 2026-07-31

- fix hub mode: compute git status server-side via services directly

## [1.10.1] - 2026-07-31

- changed min python version to 3.12

## [1.10.0] - 2026-07-31

- hide Git Config navbar link when PYOBS_CONFIG_GIT_ENABLED is False
- simplify git_pull: remove auto-commit/autostash logic
- warn users before pull when local changes exist
- add Revert button to discard uncommitted changes
- fix: handle malformed YAML configs gracefully
- branch badge: use span instead of <a>
- Strip git subpath prefix from file paths in git_status
- categorize changed files into New, Modified, Deleted sections
- simplify git workflow: Push auto-stages + commits + pushes
- fix: filter git porcelain v2 comment lines in status parser
- Move git config panel to its own page
- derive config directory dynamically from git settings
- filter invalid module names in list_modules consumers
- moved git panel
- fix: PR review comments
- fix: PR review improvements for git-backed config
- feat: add git-backed configuration support
- feat: git-backed config with sparse checkout on dashboard
- docs: add Git-backed configuration implementation plan

## [1.9.5] - 2026-07-26

- Gate journald PYOBS_MODULE tagging on the installed pyobs-core version

## [1.9.4] - 2026-07-26

- Fix journald log filtering for deactivated modules after PYOBS_MODULE underscore-stripping

## [1.9.3] - 2026-07-24

- Fix log-level misclassification from substring match on exception names
- Add dependabot.yml, targeting develop for PRs

## [1.9.2] - 2026-07-21

- Stagger Start All/Restart All so modules don't launch simultaneously
- Document required journal-read group for PYOBS_LOG_BACKEND=journald

## [1.9.1] - 2026-07-18

- Remove the Auto-scroll checkbox; make it purely position-based

## [1.9.0] - 2026-07-18

- Fix and improve load-older-logs: stop auto-refresh from wiping history, pause auto-scroll while reading old logs

## [1.8.0] - 2026-07-17

- Move design-doc journals into dev-docs/
- Document load-older-logs in the Sphinx manual (features/logging, api_endpoints)
- Document the load-older-logs feature in DEV_JOURNALD_LOGS.md, DEVELOPMENT.md, README.md
- Auto-load older logs when scrolling to the top of a log window

## [1.7.8] - 2026-07-15

- Add "Acknowledge All" button to the dashboard

## [1.7.7] - 2026-07-15

- Make dashboard log-level badges respect per-module Acknowledge state

## [1.7.6] - 2026-07-15

- Fix log-badge timezone mismatch causing stuck/missing Acknowledge counts

## [1.7.5] - 2026-07-15

- Prevent systemd from killing pyobs modules on web-admin restart

## [1.7.4] - 2026-07-15

- Add per-module Acknowledge action to log views

## [1.7.3] - 2026-07-15

- Fix crash when a module's comm: config references a missing {include}
- Add per-client hub tokens and document the HTTP API

## [1.7.2] - 2026-07-10

- Document package management in README
- Support git/URL-installed packages in PYOBS_MANAGED_PACKAGES

## [1.7.1] - 2026-07-09

- Add PYOBS_MANAGED_PACKAGES setting for extras and non-pyobs packages
- Fix package update logic to correctly handle pre-release installs
- Add fleet-wide package-version matrix to the Overview page

## [1.7.0] - 2026-07-09

- Add Packages page: show installed pyobs-* versions vs. PyPI latest, with update button
- Add ejabberd and PYOBS_LOG_BACKEND settings to local_settings.py.example

## [1.6.1] - 2026-07-06

- Prefix design-doc filenames with DEV_ to distinguish them from user docs

## [1.6.0] - 2026-07-05

- Add Read the Docs build config
- Restructure docs into a multi-page Sphinx manual
- Add Sphinx documentation setup

## [1.5.0] - 2026-07-05

- Auto-detect PYOBS_LOG_BACKEND from pyobsd's own config file
- Move Groups design/implementation record out to its own doc
- Revert "Add the ACL Groups storage layer (Work Plan item 9 in ACL_MATRIX.md)"
- Revert "Add a standalone Groups management page"
- Revert "Add group expand-on-save to both ACL editing surfaces (Work Plan item 10)"
- Add group expand-on-save to both ACL editing surfaces (Work Plan item 10)
- Add a standalone Groups management page
- Add the ACL Groups storage layer (Work Plan item 9 in ACL_MATRIX.md)
- Add a "New module" button to create a brand-new module config from the UI
- Add a fleet-wide Overview page, separate from the per-host Dashboard
- Document ejabberd user management in README and fix Dashboard's sidebar position
- Fix real horizontal page overflow on mobile caused by a wide ACL matrix
- Fix ACL matrix table stretching to full container width on wide screens
- Make the ACL matrix mobile-friendly and always show every module as a column
- Add "New module" button as a todo idea
- Add Kick action and redesign the Users page as a modal-free, mobile-friendly list
- Note the Users page as shipped, add write-action buttons as a follow-up idea
- Add a fleet-wide XMPP Users page and stop showing raw exceptions in unreachable-host banners
- Track ejabberdctl-sudo.sh as a reusable deploy artifact, not a local-only hack
- Implement XMPP account write actions (register/reset password/ban/unregister)
- Move global nav (Dashboard/Logs/ACL Matrix) above Hosts in sidebar
- Design and verify EJABBERD_USER_MANAGEMENT.md: XMPP account writes from pyobs-web-admin
- Make All Logs fleet-wide and rename sidebar entry to Logs
- Add fleet-wide All Logs view merging logs across selectable modules
- Implement journald-backed module logging (PYOBS_LOG_BACKEND)
- Verify cross-user journald read access live, overturning the systemd-journal group assumption
- Resolve JOURNALD_LOGS.md's retention/rotation open question
- Move resolved JOURNALD_LOGS.md open questions into Design, keep Open questions to what's actually unresolved
- Resolve three open questions in JOURNALD_LOGS.md: no fallback, no per-module override, upstream bug filed
- Add JOURNALD_LOGS.md design doc for journald-backed module logging
- Document ejabberd shared-roster groups as an alternate ACL-groups storage option, and add XMPP user management as a deferred idea
- Document the ejabberd integration in README
- Fix dashboard XMPP tile to count this installation's own modules, not ejabberd's fleet-wide totals
- Fix false "connected" indicator when modules share a comm.user
- Fix a race between refreshAllStatuses and refreshEjabberd on the dashboard
- Wire ejabberd status into the dashboard and module pages (Work Plan item 7)
- Add ejabberd hub-mode delegation (Work Plan item 6)
- Add local ejabberd API endpoints (Work Plan item 5)
- Add services.get_comm_user (Work Plan item 4)
- Add modules/ejabberd.py: the ejabberd data layer (Work Plan item 3)
- Close out Work Plan item 2: ejabberd-side config is already documented
- Add ejabberd integration settings (Work Plan item 1)
- Resolve the ejabberd credential-layer question: IP-only for v1
- Reorganize design docs: DEVELOPMENT.md becomes an index
- Verify mod_http_api's security model against a real non-loopback request
- Add design doc for an ejabberd integration (dashboard + module pages)

## [1.4.0] - 2026-07-03

- Add a per-module ACL tab to module_detail, alongside the matrix modal
- Mark Groups (Work Plan items 9-12) as deferred
- Aggregate the ACL matrix across every configured hub host
- Add a graphical ACL editor to the matrix page
- Add editing affordances to the ACL matrix page
- Fix Work Plan item numbering in progress log
- Add ACL matrix page: view, template, URL, sidebar entry
- Implement ACL matrix data layer: include resolution, resolved acl, matrix scan
- Decide against live XMPP interface discovery for the ACL matrix
- Design ACL groups/profiles for the fleet-wide matrix page
- Add design doc for a fleet-wide ACL matrix page

## [1.3.0] - 2026-06-20

- Make module name a link and replace Details with Logs button on dashboard

## [1.2.0] - 2026-06-16

- Improve dashboard responsiveness on mobile
- Format RAM and CPU to 1 decimal place everywhere
- Replace dashboard card grid with sortable list view

## [1.1.2] - 2026-06-15

- log _module.yaml in module.log

## [1.1.1] - 2026-06-15

- Add total RAM and CPU usage to dashboard header stats

## [1.1.0] - 2026-06-15

- Bind gunicorn to 0.0.0.0 instead of localhost
- Use HTTPS clone URLs in README
- Add deploy.sh for production setup
- Add visual divider between running and stopped modules on dashboard

## [1.0.0] - 2026-06-14

- Sort dashboard cards: running first, then stopped, each alphabetically
- Sort sidebar modules: running first, then stopped, each alphabetically
- Fix author name and email in pyproject.toml
- Add description and author to pyproject.toml
- Update README with hub mode documentation
- Add hub feature: control remote hosts from the sidebar
- Clean up README: fix redundancy, add log counts, update project layout
- Update README with PID display and activate/deactivate on detail page
- Show PID on module detail overview tab
- Update README with activate/deactivate feature
- Add activate/deactivate toggle for modules
- Note localhost-only binding in README nginx example
- Bind gunicorn to localhost only
- Change default port from 8000 to 8765
- Add local_settings.py.example referenced in README
- Update README with config editor and CodeMirror features
- Add YAML syntax highlighting and include links to config editor
- Update README with shared configs feature
- Add Shared section in sidebar for *.shared.yaml config files
- Update README with log stats feature
- Add log message counts per level (last 24h) to dashboard and detail page
- Update README to mention uptime display
- Add uptime display for running modules
- Update README with restart, bulk actions, and resource usage features
- Add CPU and memory usage display for running modules
- Add restart button for individual modules and Restart All on dashboard
- Add module summary stats and exclude underscore modules from Start All
- Add Start All / Stop All buttons to dashboard
- Remove placeholder image from README
- Add README with project overview and production setup guide
- Add STATIC_ROOT for nginx deployment
- Add gunicorn and systemd service file for production deployment
- Replace database auth with stateless cookie sessions
- Add login-required authentication to all views
- Responsive mobile layout: slide-in sidebar + sticky top navbar
- Click log lines to set time range filter
- Add time range filter to log viewer; persist all filter settings
- Persist log tab toggle settings in localStorage
- Preserve active tab when switching modules via sidebar
- Persist active tab in URL hash on detail page
- Color-code log lines and enable auto-refresh by default
- Fix header button alignment to the right
- Move start/stop buttons into module detail header
- Replace pyobsd dependency with direct pyobs process management
- Add local_settings.py support for local overrides
- Initial commit: Django web GUI for managing pyobs modules
