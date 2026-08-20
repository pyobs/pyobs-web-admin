# Plan: Run package updates as a background job with a live log tail, not a blocking request

Status: implemented

Landed: PR #50 ("Run package updates as a detached background job with a live log tail", commit
`81cbda3`, merged `a7fdad8`) — `update_package_start()`/`update_package_status()` and the
`pkg-update.{lock,json,log,exit}` files under `_run_dir()` (`modules/services.py`),
`api_package_update`/`api_package_update_status` (`modules/views.py`, `modules/urls.py`), the
live log tail on `templates/modules/packages.html`, and tests in `modules/tests.py`.

## Problem

`update_package()` (`modules/services.py:732`) runs `pip install --upgrade` synchronously inside
the Django request handler via `subprocess.run(args, timeout=120)`. A package that needs to
compile something (no prebuilt wheel for the host's platform, e.g. an ARM SBC) can easily exceed
120s; pip gets killed mid-install and the admin sees a bare "Timed out" failure even though the
install may have just needed more time.

In hub mode it's worse: `api_package_update` (`modules/views.py:823`) delegates to a remote host
via `_proxy(host, "POST", f"/api/packages/{name}/update/")` without an explicit `timeout=`, so it
inherits `proxy.call`'s default `timeout=10` (`modules/proxy.py:21`) — an order of magnitude
below the local 120s ceiling. Through a hub, almost any real install trips this.

Underneath both: this is a synchronous view blocking a Django worker thread for the whole install.
Even after raising the timeouts, whatever sits in front of Django in production (nginx/gunicorn)
may have its own limit and kill the HTTP response while the install keeps running server-side —
the UI reports failure on an install that actually succeeded.

## Proposal

1. `POST .../update/` starts the install detached and returns immediately — pip's own runtime no
   longer lives inside the request.
2. A new `GET .../update/status/` endpoint reports job state and a log tail; the frontend polls it
   and shows the output live (not just a spinner) so a slow compile is visible, not a black box.
3. One job at a time, host-wide, not per-package: concurrent `pip install` invocations against the
   same venv aren't safe (metadata races in `dist-info`/`RECORD`), so a second Update click while
   one is running is refused with a message naming the in-flight package, the same way an admin
   doing this by hand would just wait.
4. Hub mode proxies both endpoints like every other host-routed call. No explicit timeout override
   is needed anywhere any more — both calls return fast now that neither blocks on pip.

## Implementation

### 1. Background job primitives — `modules/services.py`

Files under `_run_dir()` (`services.py:50`, same directory module PID files already live in), all
owned by one job at a time and overwritten wholesale by the next `update_package_start`:

- `pkg-update.lock` — empty file, held via `flock(LOCK_EX | LOCK_NB)` only around the
  check-then-spawn section of `update_package_start`. Without this, two Update clicks landing on
  different gunicorn workers close enough together could both observe "no job running" before
  either has written its lock metadata, and both spawn a `pip install` against the same venv —
  exactly the race the one-job-at-a-time guarantee above exists to prevent. A second request that
  can't acquire the flock reports "already updating" immediately rather than blocking.
- `pkg-update.json` — metadata: `{"name": <pkg>, "pid": <shell pid>, "pid_create_time": <float>,
  "started_at": <ts>}`.
- `pkg-update.log` — pip's combined stdout+stderr, written directly by shell redirection (see
  below) so it fills in live, not buffered and dumped at the end.
- `pkg-update.exit` — plain text return code, written by the spawned shell once pip exits; its
  absence is exactly "still running" (or "interrupted", see below).

`pid_create_time` (`psutil.Process(pid).create_time()`, recorded at spawn) guards against PID
reuse: a bare liveness check (`_is_alive(pid)`, i.e. `os.kill(pid, 0)`) would report an unrelated
process as "still running" forever if the host rebooted and the OS later handed the tracked pid
to something else. `_pkg_update_process_alive(pid, expected_create_time)` compares the live
process's actual creation time against the recorded one (falling back to plain liveness if the
field is missing, e.g. an older lock file). This is deliberately not a wall-clock "how long has
this been running" cutoff — that would reintroduce exactly the arbitrary-timeout guessing this
whole design exists to avoid; comparing process identity via creation time answers "is this
really still my job" without needing to guess at how long a slow compile is allowed to take.

`_build_update_args(name, installed_version) -> list[str]` — factor the argument-building out of
today's `update_package` unchanged (the `pyobs*`/allow-list name check, `--pre` for a pre-release
`installed_version`, `--force-reinstall --no-deps` for VCS-managed packages, `_install_spec_for`).
This becomes the only caller of that logic; the old blocking `update_package` is deleted.

`update_package_start(name, installed_version) -> tuple[bool, str]`:

- Acquire `pkg-update.lock` via non-blocking `flock`; if that fails, another request is inside
  this same critical section right now — report "already updating" using whatever the metadata
  file currently says (best-effort; it will be accurate by the time the other request finishes).
- Inside the lock: refuse if a job is already active (metadata present, exit file absent,
  `_pkg_update_process_alive(pid, pid_create_time)` true) — `False, f"Already updating
  {other_name}"`.
- Otherwise `_build_update_args(...)`, truncate/recreate log+exit, and spawn:
  `subprocess.Popen(["sh", "-c", f"{shlex.join(args)} > {log} 2>&1; echo $? > {exit_file}"],
  start_new_session=True, stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL)`. The shell (not us)
  redirects pip's output straight into the log file and appends the exit code once pip finishes —
  no wrapper script, no capturing-then-writing after the fact.
- Write the metadata file (pid, `psutil.Process(pid).create_time()`, started_at), release the
  flock, return `True, f"Started updating {name}"` immediately. The flock is held only for this
  fast check-and-spawn section, never for the install itself.

`get_package_update_status() -> dict`:

- No metadata file → `{"active": False}`.
- Metadata present, exit file present → job finished: `{"active": False, "name", "state":
  "success"/"failed", "log": <tail>}`. Left in place until the next `update_package_start`
  overwrites it, so a page reload right after completion still shows the result.
- Metadata present, exit file absent, `_pkg_update_process_alive(pid, pid_create_time)` true →
  `{"active": True, "name", "state": "running", "log": <tail>}`.
- Metadata present, exit file absent, process not alive (by pid liveness, or same pid but a
  mismatched create_time — see the PID-reuse note above) → `{"active": False, "name", "state":
  "interrupted", "log": <tail>}`. Reported distinctly from "failed" so the admin knows to just
  retry rather than debug pip output that doesn't exist.
- `<tail>` is the log file's last ~4000 chars, read fresh every call — cheap, and correct even if
  the poll lands on a different Django worker than the one that started the job, since all state
  lives on disk rather than in worker memory. `get_package_update_status` never touches the flock
  — it only reads, and reads are safe to run concurrently with the brief write section.

### 2. Views — `modules/views.py`

- `api_package_update` (existing route) calls `update_package_start` instead of the blocking
  `update_package` and returns immediately. Hub-mode branch (`_proxy(...)`, `views.py:814`)
  unchanged — no timeout override needed now that the call is fast.
- New `api_package_update_status` (`GET`), same host-routing shape as `api_packages`: proxy when
  `_active_host(request)` is set, else `JsonResponse(services.get_package_update_status())`.

### 3. URLs

Add a route for the new status endpoint next to the existing package routes (e.g.
`packages/update/status/`), named so `{% url %}` can reference it from the template.

### 4. Frontend — `templates/modules/packages.html`

- A shared log panel (collapsible `<pre>`) below the table, hidden by default — shared because
  only one job can be active host-wide.
- `updatePackage(name, btn)`: POST start; if refused (`ok: false`), `alert()` as today. If
  accepted: show the panel ("Updating `<name>`…"), poll `GET .../update/status/` every ~1.5s,
  replacing the `<pre>` content with the returned log tail each time. On a terminal state, stop
  polling; on success, hide the panel and `loadPackages()` as today; on failure/interrupted, leave
  the panel open showing the full output instead of the current `alert()` (there's real output
  worth reading, not a one-line error).
- `loadPackages()` also does one `GET .../update/status/` on load — if a job is already active
  (admin reloaded mid-install, or opened the page while someone else's update is running), resume
  polling and show the panel instead of leaving the page looking idle. This is what makes an
  in-flight install survive a browser refresh or a web-admin worker restart.

### 5. Proxy — `modules/proxy.py`

No change. `proxy.call`'s default `timeout=10` is fine for both the start call and each status
poll now that neither blocks on pip.

## Tests

In `modules/tests.py`:

- `update_package_start`: returns immediately even for a deliberately slow fake pip (`sleep 1`
  via a stubbed `_build_update_args`); a second call while the first is still running is refused
  and names the in-flight package; log file contains the fake command's output after it
  completes; the metadata file's `pid_create_time` matches the spawned process's actual
  `psutil.Process(pid).create_time()`.
- `update_package_start` under contention: a second call refused while a test-held `flock` on
  `pkg-update.lock` simulates a concurrent request inside the same critical section — exercises
  the atomic-lock path directly rather than only the already-written-metadata path.
- `get_package_update_status`: no-job, running (pid alive + matching create_time, no exit file),
  success (exit file `0`), failed (exit file nonzero), interrupted-gone (lock present, exit file
  absent, pid not alive), interrupted-reused-pid (pid alive but recorded `pid_create_time`
  doesn't match, using this test process's own pid) — each asserted from files written directly
  in the test rather than waiting on a real spawn.
- `api_package_update`: returns `ok: true` immediately (mock `update_package_start`); hub-mode
  branch still proxies.
- `api_package_update_status`: returns the service dict; hub-mode branch proxies.

## Consequences

- **Good:** removes both timeout bugs outright — pip's own runtime is no longer bounded by
  anything this app chooses.
- **Good:** survives a web-admin worker restart or a browser refresh mid-install, since job state
  lives in files under `_run_dir()`, not in request/worker memory — the motivating case (a
  redeploy landing during a long compile) is handled, not just made less likely.
- **Neutral:** one job at a time, host-wide rather than per-package, because concurrent `pip
  install` invocations against the same venv aren't safe. A second Update click while one is
  running is refused, not queued — matches how an admin doing this by hand would behave anyway.
  The refusal itself is race-free: the check-then-spawn section is covered by a non-blocking
  `flock`, not a plain file-existence check, so two requests landing on different gunicorn
  workers microseconds apart can't both pass the check and spawn two installs.
- **Neutral:** a rebooted host reusing the tracked pid for an unrelated process would make a bare
  `os.kill(pid, 0)` liveness check report a long-dead job as "running" forever. Recording
  `pid_create_time` at spawn and comparing it against the live process's actual creation time on
  every read catches this without needing a wall-clock staleness cutoff (which would just
  reintroduce the arbitrary-timeout guessing this whole design avoids).
- **Accepted trade-off:** the spawned `sh` isn't reaped with an explicit `os.wait()` in the
  request handler; it relies on Python's own `subprocess._cleanup()`, which runs opportunistically
  whenever this same worker next spawns another subprocess (git commands, module start/stop,
  another update — all common in this app). A full detach-and-self-reap daemon, the way pyobs
  modules do via `python-daemon`'s double fork, would close this off completely, but is more
  machinery than a low-frequency admin action (a handful of updates, ever) justifies.
