# Plan: Clone git repos to separate source dir with symlink to config

Status: planned

Issues: #38

## Problem

The current `git_clone()` puts the repository directly into `PYOBS_CONFIG_GIT_ROOT`, which
defaults to `PYOBS_CONFIG_DIR`. This means the `.git` directory lives inside the config path
that pyobs-core reads, making the setup fragile and harder for `pyobsd` to locate configs
without knowing about the git arrangement.

The current layout from `git-backed-configuration.md`:

```
PYOBS_CONFIG_GIT_ROOT     = /opt/pyobs/config          # contains .git, cloned here
PYOBS_CONFIG_GIT_SUBPATH  = sites/obs1                  # sparse-checkout path
PYOBS_CONFIG_DIR          = /opt/pyobs/config/sites/obs1  # pyobs reads/writes here
```

Every pyobsd startup path that resolves `PYOBS_CONFIG_DIR` now needs to handle the case where
the directory is actually inside a git working tree with sparse-checkout constraints.

Goal: isolate `.git` from the config tree that pyobs reads.

## Proposal

New layout:

```
/opt/pyobs/src/pyobs-monet/                     # git repo root (contains .git)
├── configs/
│   └── south/
│       └── monet/                              # actual config files
└── ...
/opt/pyobs/config -> /opt/pyobs/src/pyobs-monet/configs/south/monet  # symlink
```

`PYOBS_CONFIG_DIR` becomes a symlink pointing into the git repo's config subpath. pyobs and
pyobsd see the same path they always did, but it's now a transparent redirect into the
separated source tree.

## Implementation

### 1. New setting — `settings.py`

Add `PYOBS_CONFIG_GIT_SOURCE_DIR` with default `"/opt/pyobs/src"`. This is the parent
directory where repos clone to, one subdirectory per repo name:
`<source_dir>/<repo_name>`.

Derive `PYOBS_CONFIG_GIT_ROOT` from it when `_ROOT` is not explicitly set:
`_git_repo_dir()` returns `Path(PYOBS_CONFIG_GIT_SOURCE_DIR) / repo_name`.

Keep `_GIT_ROOT` for override but document it as optional. The derived path takes the
default and the explicit setting still wins.

### 2. `_repo_name()` helper — `services.py`

Extract the repo name from `PYOBS_CONFIG_GIT_REPO`. Strip trailing `.git` and take the
last path segment:

| URL | Result |
|-----|--------|
| `https://github.com/pyobs/pyobs-config.git` | `pyobs-config` |
| `git@github.com:pyobs/pyobs-config.git` | `pyobs-config` |
| `/absolute/path/to/my-config.git` | `my-config` |

### 3. `_ensure_symlink()` helper — `services.py`

Creates the symlink from `PYOBS_CONFIG_DIR` to `<source_dir>/<repo_name>/<subpath>`
(where subpath comes from `PYOBS_CONFIG_GIT_SUBPATH`). Idempotent:

- If path is already a symlink and points to the expected target, do nothing
- If path is already a symlink and points elsewhere, return error
- If path exists and is not a symlink (directory or file), return error
- Otherwise create parent dirs if needed, create the symlink

Return `(success, message)` for consistency with the rest of the git API.

### 4. Rewrite `_git_repo_dir()` — `services.py:91`

When `_GIT_ROOT` is not explicitly set and git is enabled, derive it as
`Path(PYOBS_CONFIG_GIT_SOURCE_DIR) / _repo_name()`. The explicit
`_GIT_ROOT` still takes priority for backward compatibility and override cases.

Current behavior (line 97-105) already falls back to deriving from `_config_dir()` and
subpath when no explicit root. Add the source-dir derivation as the new primary path when
`_ROOT` is empty.

### 5. Rewrite `_config_dir()` — `services.py:34`

If `PYOBS_CONFIG_DIR` is a symlink, resolve it through to the actual target. Otherwise,
with git enabled and root/subpath set, the existing `<root>/<subpath>` lookup applies. The
fallback to `Path(settings.PYOBS_CONFIG_DIR)` stays for non-git and pre-symlink cases.

### 6. Update `git_clone()` — `services.py:1154`

Clone into `<source_dir>/<repo_name>` from `_git_repo_dir()`, then call
`_ensure_symlink()` after a successful clone. If the symlink creation fails, rollback
the clone directory and return error, same as the current sparse-checkout rollback logic.

Change `_git_config_ok()` to validate the symlink target, not the raw config dir, since the
"invariant: config dir is inside repo" now holds through the symlink rather than the
directory itself being inside the repo tree.

### 7. Hook `_ensure_symlink()` on startup — `modules/apps.py`

Add a `ready()` method to `ModulesConfig` that calls `_ensure_symlink()` when git is
enabled. Skips gracefully if the repo or symlink target does not exist yet. This ensures
the symlink is restored after deployment or app restart, even when no new clone happened.

### 8. Update settings comments — `settings.py:126-151`

Replace the current layout diagram with the new symlink layout. Document the relationship
between `_SOURCE_DIR`, derived `_ROOT`, and the `PYOBS_CONFIG_DIR` symlink. Keep existing
`_REPO`, `_BRANCH`, `_AUTHOR_NAME`/`_EMAIL` documentation unchanged.

## Tests

New test cases in `modules/tests.py`:

- `_repo_name()`: extract correctly from HTTPS URLs, SSH URLs, and absolute path repos
- `_ensure_symlink()`: creates symlink when path is missing, does nothing when symlink
  exists with correct target, errors when symlink has wrong target, errors when path is
  a regular directory
- `_git_repo_dir()`: derives from `SOURCE_DIR` when `ROOT` is not set, uses explicit
  `ROOT` when set
- `git_clone()`: creates symlink after successful clone, rolls back directory on symlink
  failure, symlink error propagates to caller
- `ready()` hook: calls `_ensure_symlink()` when git is enabled, skips when disabled,
  skips when repo does not exist

Existing git test suite should pass with adapted mocks for the new setting and derived
paths.

## Consequences

- **Good:** `pyobsd` can resolve `PYOBS_CONFIG_DIR` to actual configs without knowing about
  the git setup, following a standard path.
- **Good:** `.git` directories are isolated from the config trees pyobs reads, removing the
  fragile assumption that the config directory happens to be inside a git working tree.
- **Good:** source repos are in a single parent directory, making backup and migration easier.
- **Neutral:** `PYOBS_CONFIG_DIR` is now a symlink instead of a directory. Existing tools
  and `os.path` checks that don't follow symlinks would need adjustment, but `pathlib.Path`
  operations already resolve through symlinks by default.
- **Risk:** symlink creation during `git_clone()` adds a new failure mode. The rollback
  strategy (remove the clone directory on symlink failure) mirrors the existing sparse-checkout
  rollback, keeping risk contained.
- **Out of scope:** updating deployment scripts or systemd unit files to create
  `PYOBS_CONFIG_GIT_SOURCE_DIR` before the app starts. That's environment configuration,
  not code.