# Git-backed Configuration – Implementation Specification

This document combines the architecture and implementation guidance discussed in the conversation.

## Contents

- Part 0 – Repository Analysis
- Part 1 – Architecture & Overall Design
- Part 2 – services.py
- Part 3 – API Layer
- Part 4 – Dashboard
- Part 5 – Testing
- Part 6 – Implementation Order

> **Note:** This document is based on the uploaded `pyobs-web-admin` repository and the implementation decisions discussed in this chat.

---

# Part 0 – Repository Analysis

The implementation should preserve the existing architecture:

```
Templates
    ↑
views.py
    ↑
services.py
    ↑
Filesystem
```

- All Git logic belongs in `modules/services.py`.
- `views.py` should remain a thin wrapper.
- Reuse `_config_dir()` for repository discovery.
- Use `pathlib.Path`.
- Reuse the existing Hub proxy mechanism.
- Add endpoints under the existing `/api/` namespace.
- Add a dashboard card instead of a new page.
- Mock `subprocess.run()` in tests.
- Before implementing Git, refactor configuration writes into a single internal helper (if one does not already exist) and call `_git_auto_stage()` there.

---

# Part 1 – Architecture

Use Git as a persistence backend, not as a Git client.

Repository layout:

```
/opt/pyobs/config/
    .git/
    sites/
        obs1/
```

```
PYOBS_CONFIG_DIR = /opt/pyobs/config/sites/obs1
PYOBS_CONFIG_GIT_SUBPATH = sites/obs1
```

Repository root is computed from `PYOBS_CONFIG_DIR` and `PYOBS_CONFIG_GIT_SUBPATH`.

Rules:

- No symlinks.
- No copied repositories.
- No `git init`.
- Use sparse checkout.
- Never execute Git outside `services.py`.

---

# Part 2 – services.py

Add settings:

- PYOBS_CONFIG_GIT_ENABLED
- PYOBS_CONFIG_GIT_REPO
- PYOBS_CONFIG_GIT_SUBPATH
- PYOBS_CONFIG_GIT_BRANCH

Private helpers:

- `_git_enabled()`
- `_git_subpath()`
- `_git_repo_dir()`
- `_git_run()`
- `_git_auto_stage()`

Public API:

- `git_repo_exists()`
- `git_clone()`
- `git_fetch()`
- `git_status()`
- `git_stage_all()`
- `git_commit()`
- `git_pull()`
- `git_push()`

Implementation notes:

- Use `subprocess.run(..., cwd=repo, capture_output=True, text=True, timeout=60)`.
- Set `GIT_TERMINAL_PROMPT=0`.
- Prefer `git status --porcelain=v2 --branch` for status parsing.
- Automatically stage changes after successful configuration writes.
- `git_pull()` should auto-stage and auto-commit before `git pull --autostash`.

---

# Part 3 – API Layer

Add endpoints:

- GET `/api/config/git/status/`
- POST `/api/config/git/clone/`
- POST `/api/config/git/fetch/`
- POST `/api/config/git/pull/`
- POST `/api/config/git/push/`

Views should only:

1. Determine local vs Hub.
2. Proxy if necessary.
3. Call `services.git_*()`.
4. Return `JsonResponse()`.

---

# Part 4 – Dashboard

Add a Git card to `templates/modules/dashboard.html`.

Display:

- Branch
- Remote
- Subpath
- Ahead / Behind
- Last commit
- Clean / Dirty

Buttons:

- Clone (if missing)
- Refresh (fetch + status)
- Pull
- Push

Refresh the card after every operation.

---

# Part 5 – Testing

Mock every `subprocess.run()` call.

Test:

- repository root calculation
- clone
- sparse checkout
- status parsing
- fetch
- pull
- push
- auto-stage
- Git disabled
- failure cases

No real repositories or network access.

---

# Part 6 – Implementation Order

1. Add settings.
2. Refactor configuration writes into one helper.
3. Implement Git helpers.
4. Implement public Git API.
5. Hook auto-stage into write helper.
6. Add API endpoints.
7. Add dashboard.
8. Add tests.
9. Run existing test suite.

## Acceptance Criteria

- Existing behaviour unchanged when Git is disabled.
- Sparse checkout supported.
- No symlinks.
- No copied repositories.
- All Git operations centralized in `services.py`.
- Dashboard supports Clone, Refresh, Pull and Push.
- Configuration writes automatically stage changes.
- Full test suite passes.
