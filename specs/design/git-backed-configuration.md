# pyobs-web-admin: Git-backed configuration

## Status

**Implemented.** Git as an optional persistence backend for the config tree pyobs itself reads
(`PYOBS_CONFIG_DIR`), with a dedicated sidebar page (`/git-config/`, not a dashboard card as
originally planned) for status and clone/fetch/pull/push/reset. Off by default
(`PYOBS_CONFIG_GIT_ENABLED = False`). Not covered in `README.md` yet. See **Open questions**
for two known gaps (`git_pull()` not auto-staging first; `git_config_page` ignoring the enabled
flag) that haven't been fixed.

## Motivation

Configuration writes (`save_config`, module create/delete, ACL/comm edits, etc.) land as plain
file writes under `PYOBS_CONFIG_DIR`. There was no way to track, review, or push that history
anywhere — every change was local-only and lost the moment the disk was.

## Design

### Repository layout

Git as a persistence backend, not a general-purpose Git client. Sparse checkout keeps only the
configured subpath in the working tree:

```
PYOBS_CONFIG_GIT_ROOT     = /opt/pyobs/config          # contains .git, cloned here
PYOBS_CONFIG_GIT_SUBPATH  = sites/obs1                  # sparse-checkout path inside the root
PYOBS_CONFIG_DIR          = /opt/pyobs/config/sites/obs1  # what pyobs itself reads/writes
```

`PYOBS_CONFIG_GIT_ROOT` is explicit and constant, not derived from `PYOBS_CONFIG_DIR` — this
decouples the repo location from the pyobs config path (which may itself point inside the
sparse checkout) and avoids recursive-clone footguns if `PYOBS_CONFIG_DIR` changes later.
`_git_config_ok()` (`services.py:67`) enforces that `PYOBS_CONFIG_DIR` is actually inside the
git root before any git operation runs.

Settings (`pyobs_web_admin/settings.py:116`): `PYOBS_CONFIG_GIT_ENABLED`, `_ROOT`, `_SUBPATH`,
`_REPO`, `_BRANCH` (default `"main"`), plus `PYOBS_CONFIG_GIT_AUTHOR_NAME`/`_EMAIL` for the
identity auto-commits use (git has no fallback identity of its own — without these, or a global
`git config user.name/user.email` already set for whichever system user runs
pyobs-web-admin, commits fail outright with "Author identity unknown").

### services.py

All git logic lives here — `views.py` stays a thin wrapper, `subprocess.run(..., cwd=repo_dir,
capture_output=True, text=True, timeout=...)`, `GIT_TERMINAL_PROMPT=0` always set.

Private: `_git_enabled()`, `_git_root()`, `_git_config_ok()`, `_git_subpath()`,
`_git_repo_dir()`, `_git_run()`, `_git_auto_stage()`.

Public: `git_repo_exists()`, `git_clone()` (refuses if the root already has a `.git` or
non-empty contents; sets up `sparse-checkout --cone` + `set <subpath>` when a subpath is
configured, rolling back the clone if either step fails), `git_fetch()`, `git_status()`
(branch, ahead/behind vs `origin/<branch>`, last commit hash/time, and
modified/new/deleted file lists parsed from `git status --porcelain=v2 --branch`, with the
sparse-checkout subpath prefix stripped so paths read relative to `PYOBS_CONFIG_DIR`),
`git_stage_all()`, `git_commit(message)` (passes author identity via `-c user.name=…` /
`-c user.email=…` per-invocation rather than mutating global git config; `--allow-empty`),
`git_pull()`, `git_push()`, `git_init_if_needed()` (clone if missing, else no-op),
`git_reset()` (`reset --hard HEAD`, discards uncommitted changes).

`_git_auto_stage()` runs after every config write (`services.py:1140`, called from the shared
write helper) — best-effort, never raises, so a git hiccup never blocks a config save.

### API layer (`modules/urls.py:62`)

`GET /api/git/status/`, `POST /api/git/clone/`, `/fetch/`, `/pull/`, `/push/`, `/reset/`. Views
only resolve local-vs-hub, proxy if needed, call the matching `services.git_*()`, and return
`JsonResponse`.

### UI

A dedicated **Git Config** sidebar page (`/git-config/`, `templates/modules/git_config.html`,
gated on `git_enabled` from the context processor, `modules/context_processors.py:63`) — not
the dashboard card the original plan specified. Shows branch, ahead/behind, clean/dirty, and
modified/new/deleted file lists; buttons for Clone (if missing), Refresh (fetch + status), Pull,
Push, Reset, each disabled when meaningless (`views.py:203`: pull disabled with no branch or
nothing behind; push disabled with no branch or nothing to push; reset disabled when clean).

### Testing

`modules/tests.py` mocks every `subprocess.run()` call — no real repositories or network access.
Covers repo-root calculation, clone (including sparse checkout), status parsing, fetch, pull,
push, auto-stage, git-disabled no-ops, and failure paths.

## Open questions

- **`git_pull()` (`services.py:1344`) is a bare `git pull`** — the original plan called for
  auto-staging and auto-committing local changes first, then `pull --autostash`. As implemented,
  clicking Pull with uncommitted local changes can fail or conflict instead of merging safely.
  Not yet fixed.
- **`git_config_page` (`views.py:185`) hardcodes `ctx["git_enabled"] = True`**, ignoring
  `PYOBS_CONFIG_GIT_ENABLED`. The sidebar link correctly hides when the feature is off (it reads
  the context processor's real value), but the page itself renders as if enabled if visited
  directly by URL while disabled. Not yet fixed.
- The local-vs-hub branches in `git_config_page` (`views.py:191-197`) currently do the exact
  same thing (`services.git_status()` either way) — hub-mode proxying looks unfinished/dead
  code, unlike the proxy pattern the rest of this app follows (e.g. `api_config`).
- Not documented in `README.md` — no user-facing section describing setup or the sidebar page.
