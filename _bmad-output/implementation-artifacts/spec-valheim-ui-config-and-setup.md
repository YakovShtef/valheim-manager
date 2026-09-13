---
title: 'Zero-Edit Install with a First-Run Setup Wizard'
type: 'feature'
created: '2026-09-12'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: 'NO_VCS'
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Installing the manager today means copying three example files, generating a bcrypt hash with a CLI tool, inventing a `SESSION_SECRET`, and editing the game password — all before the stack will boot. That hand-work is the biggest barrier to using any of it.

**Approach:** Make `docker compose up -d` the entire install. The manager boots unconfigured, logs a one-time setup URL, and a browser wizard creates the admin account, generates its own session secret, and writes the initial game settings — so no file is hand-edited.

## Boundaries & Constraints

**Always:**
- A clean `docker compose up -d` on a tree with no env or state files must reach a usable login with no step beyond opening the logged setup URL.
- The wizard opens only while no admin exists **and** only with the one-time token logged at boot. Once setup completes it is permanently closed and the token is void.
- Manager credentials live in a state file on a manager-owned volume at mode `0600`, never in a hand-edited env file. `ADMIN_USER`/`ADMIN_PASSWORD_HASH`/`SESSION_SECRET` env vars still take precedence and skip setup entirely.
- The manager creates a default settings file only when none exists, and writes settings atomically (temp-then-replace), preserving comments, blank lines, key order and unrecognised keys.
- Password hashing reuses `hash_password`, including its 72-byte bcrypt guard and argon2 alternative.
- No world exists until the operator presses Start; the wizard must not start the server.

**Never:**
- No post-install settings panel and no world modifiers in this build — the wizard writes initial values only (deferred).
- No world upload, delete, switch or seed selection; Valheim has no `-seed` argument, so a first Start still fixes a random seed.
- No "Backup now", mod toggles, admin/ban-list editing, or backup restore — all stay deferred.
- Never weaken the configured path: a manager with credentials present must still refuse to boot on a bad hash or short secret.
- Never log the admin password, the hash, or the session secret.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Clean install | No env or state files, `docker compose up -d` | Boots, logs a setup URL with a one-time token, creates a default settings file; all other routes redirect to setup | Unwritable state volume: refuse to boot, naming the path |
| Bad token | `/setup` with a wrong, missing or reused token | Refused; no admin created; the real token stays usable | Generic refusal revealing nothing about whether a token exists |
| Completing setup | Valid token, admin username + password, initial server fields | Admin and generated secret persisted `0600`; settings written; operator lands authenticated; `/setup` closed | Password over 72 bytes refused with argon2 offered, as today |
| Revisiting setup | Configured manager, `/setup` reopened | Refused even with the original token; nothing overwritten | Redirect to login |
| Pre-configured by env | All three credential env vars set | Setup never opens, no token logged, login behaves as the previous build | A bad env hash still fails loudly at boot |
| Restart after setup | Manager restarted or recreated | Credentials load from the state file; existing sessions stay valid | Corrupt state file refuses to boot and names it rather than reopening setup |
| Operator's own settings file | A settings file present before first boot | Left byte-identical — not overwritten, not reformatted | Unreadable or a directory: the existing named error |

</frozen-after-approval>

## Code Map

- `manager/app/auth.py` -- `SessionAuth` raises `AuthConfigError` when unconfigured; `hash_password` (with `BCRYPT_MAX_BYTES`) and `validate_password_hash` are reused unchanged. Leave the configured-path validation alone.
- `manager/app/main.py` -- `create_app` builds `SessionAuth` eagerly, so an unconfigured boot cannot start; that is the change. Reuse `config_from_env`, `session_of`/`require_session`/`require_same_origin`, and the `ConfigError` pattern.
- `manager/app/settings_store.py` -- the only reader of the game env file. **An earlier run of this spec was interrupted partway and already added** the atomic `write()`, the quoting rules, and `DEFAULT_SETTINGS_TEXT`/`ensure_default`. Re-read the file and build on or correct what is there rather than rewriting it blind; it is not yet covered by tests. Keep `parse_env_text`'s tolerance of `export `, quotes and inline comments, and the directory-detection error.
- `manager/app/state_store.py` -- **already exists from that interrupted run** (~224 lines: the `0600` JSON document, secret generation, load/save). Nothing imports it yet, so it is inert and unverified — review it against this spec's constraints before wiring it in, and fix it rather than duplicating it.
- `docker-compose.yml` -- the manager *requires* `manager.env` (`env_file: format: raw`) and mounts the settings file `:ro`; both change. Mount a settings *directory*, not a single file, so a missing file cannot become a host directory; add the state volume.
- `manager/tools/hash_password.py` -- stays as the escape hatch for env-var configuration; the README should stop presenting it as required.
- Unchanged: `docker_control.py` (the wizard never touches the container), the socket proxy, log and status paths.

## Tasks & Acceptance

**Execution:**
- [x] `manager/app/state_store.py` -- load/save the `0600` state file (admin user, hash, session secret, setup-completed), generating the secret via `secrets` on first boot — **exists already from an interrupted run; verify against the constraints above and correct, do not rewrite from scratch**
- [x] `manager/app/settings_store.py` -- atomic comment-preserving `write(updates)` and default-file creation that never overwrites an existing file — **partially present from that same run; confirm it actually preserves comments, ordering and unknown keys, and that it is atomic**
- [x] `manager/app/setup.py` -- new: token generation and constant-time check, wizard routes, admin creation via `hash_password`, permanent closure once complete
- [x] `manager/app/main.py` -- resolve credentials from env then state file; boot unconfigured rather than raising; redirect other routes to `/setup` while unconfigured; log the setup URL and token
- [x] `manager/app/templates/setup.html` -- wizard: admin fields, initial server fields, and a "no world exists until you press Start" note
- [x] `docker-compose.yml` + `manager/.env.example` + `valheim.env.example` -- drop the required `manager.env`, mount the settings directory read-write, add the state volume, document env vars as an optional override
- [x] `README.md` -- replace the multi-step install with `docker compose up -d` plus the logged URL; keep env vars as the advanced path; restate that the seed is random and fixed on first Start
- [x] `manager/app/tests/test_edge_cases.py` -- cover the matrix: token refusal and reuse, closure after completion, env vars skipping setup, state file mode and round-trip, corrupt state refusing boot, atomic write preserving comments and unknown keys, existing settings file untouched

**Acceptance Criteria:**
- Given no env or state files, when the operator runs `docker compose up -d`, then a setup URL with a one-time token is logged and every other route redirects to setup.
- Given that URL, when the admin username, password and initial server fields are submitted, then the operator lands authenticated, the state file is mode `0600`, and the settings file holds their values.
- Given setup completed, when `/setup` is reopened with the original token, then it is refused and nothing is overwritten.
- Given all three credential env vars are set, when the manager boots, then no token is logged, `/setup` is unavailable, and login matches the previous build.
- Given the manager is restarted after setup, when the page is reloaded, then the session is still valid and setup stays closed.
- Given a settings file the operator wrote, when the manager boots, then it is byte-identical afterwards.
- Given setup completed but Start never pressed, when the stack is inspected, then no game container and no world exist.

## Implementation Notes

**Carried-over code, verified and corrected.** `state_store.py` was sound; the
corrections were a `version` guard on load (a newer file must not be half-read by an
older manager), directory detection up front so Windows and Linux give the same
error, `created_at` preserved across a re-save, `exists()` removed so no caller can
treat an unreadable file as "not set up yet", and the uid hint retargeted at
`MANAGER_UID`. `settings_store.py`'s write path had one real bug: `_unquote` and
`_split_trailing_comment` both asked "is the *whole* right-hand side quoted?", so a
quoted value followed by an inline comment (`NAME="My Server" # note`) read back with
its quotes attached — and any value the writer had to quote onto such a line did not
survive a round trip, breaking `format_env_value`'s stated contract. Both now share
one `_split_raw_value`, and an empty value written next to a comment is rendered `""`
rather than `KEY= # note`, which would have read back as the comment text.

**Partial env credentials refuse to boot.** The previous build silently ignored
`ADMIN_PASSWORD_HASH` unless the other two were also set. With a wizard in play that
would hand an operator an admin-creation page while their credentials sat unused, so
it is now all three or none.

**The settings write happens before the credential save.** A failure writing settings
(an unwritable bind mount) therefore leaves setup open and retryable on the same URL,
rather than closing the wizard over a half-finished install. `SessionAuth` is also
built from the prepared state *before* anything is written, so the manager can never
persist credentials it would then refuse to load.

**Logging is configured explicitly.** Uvicorn configures only its own loggers and
leaves the root logger without a handler, so the setup URL — the one thing a clean
install cannot proceed without — would otherwise never reach `docker compose logs`.

**Native Linux ownership.** Docker creates a missing bind-mount source as root, so
`./settings` may not be writable by the manager's uid 10001 on a native Linux host.
The state volume is unaffected (a named volume inherits the image's ownership), so
the credential half of the install always works; `MANAGER_UID`/`MANAGER_GID` and a
`chown` are the documented fixes, and the wizard's error names the path.

## Spec Change Log

## Review Triage Log

**Round 1 (16 findings, all fixed).** The one that changed the shipped behaviour
most: the wizard used to accept an empty join password. `valheim_server.x86_64`
refuses to start without 5+ characters regardless of `SERVER_PUBLIC`, so the
zero-effort path built a container that exited immediately and showed only
"stopped". The wizard now requires it, and `/api/start` and `/api/restart` refuse
with a named cause when a hand-edited file leaves it short — the first check in this
build that reads the settings file *for* Start rather than only to display it.

Two were regressions the previous round introduced: replacing the readiness scan's
line cap with `tail="all"` made every negative scan re-read the whole run's log on
every poll (now throttled per run, with the log pump still flipping readiness within
a poll), and the `_pump` dedupe relied on a fixed 2000-entry set that SteamCMD's
first-run burst can overflow inside one second, evicting exactly what the next
whole-second re-read needed (now a `(epoch, raw)` high-water mark).

The rest: `UnicodeDecodeError` escaping `SettingsFileError` on a non-UTF-8 settings
file (the one file the design invites a Windows operator to edit); readiness leaking
across runs from a stale `since` window; password hashing on the event loop;
`{"force": "false"}` reading as force; `STATUS_EVERY_N_POLLS` documented and read but
never passed through by Compose; a banner that never cleared on recovery; `mkstemp`
escaping as a bare `OSError`; no `fsync` of the parent directory after `os.replace`;
missing `cap_drop`/`no-new-privileges`; an uncapped game-container log; and a
`/healthz` rationale that was simply untrue (Engine and Compose act on process exit,
never on health status — the real reason is not reporting a false failure).

The suite grew from 98 to 204 as part of this, mostly closing places where deleting
real behaviour left it green: the production controller wiring was never executed,
`config_from_env`'s non-numeric reads were never exercised through the environment,
no test provoked a failure *inside* `commit()`, settings-file mode preservation was
unasserted, and `/login`'s origin check had no negative case.

Pass 1 (`review_loop_iteration: 0`). Three layers: `blind-hunter` (17), `edge-case-hunter` (12),
`verification-gap` (6 gaps + 1 other). No `intent_gap` and no `bad_spec`, so no loopback.
Verification-gap rows arrive pre-verified per that layer's evidence rules.

| # | Finding (location) | Verdict | Evidence | Route |
|---|---|---|---|---|
| 1 | Wizard invites an empty join password, which cannot boot the server (`setup.py:53`, `setup.html`, `DEFAULT_SETTINGS_TEXT`) | high | Upstream is explicit: "SERVER_PASS must be at least 5 characters long. Otherwise `valheim_server.x86_64` will refuse to start!", and it applies regardless of `SERVER_PUBLIC` (no passwordless option without ValheimPlus). So the zero-effort path — accept the wizard's "leave it empty to pick one later", then follow the README's "press Start" — creates a container that exits at once, with the UI showing only "stopped / Container exited" and no cause. Defeats this build's whole Intent | patch P1 |
| 2 | A non-UTF-8 settings file makes `UnicodeDecodeError` escape `SettingsFileError` (`settings_store.py` read + write) | medium | Measured: a latin-1 accent *and* a Notepad "Unicode" (UTF-16) save both escape in `read()` **and** `write()` → dashboard 500 and the log pump dies. This is the one file the design invites the operator to edit on the host, and the host here is Windows | patch P2 |
| 3 | Readiness rescan re-reads the entire run's log every ~2 s per tab, forever, when the ready line never arrives (`docker_control.py:_is_ready`) | medium | Confirmed: only the *positive* result is cached, and the previous review's fix replaced the 400-line cap with `tail="all"` — so this is a regression that fix introduced. Compounded by no `logging:` size cap on the `valheim` service, so the file being re-read grows unbounded | patch P3 |
| 4 | Cross-run readiness contamination (`docker_control.py:mark_ready_if_match`) | medium | Confirmed by inspection: the run key comes from the container's *current* `StartedAt`, but the lines handed in can predate a restart (the pump holds a `since` from the previous run), so a still-loading new run is marked ready. The UI then claims "ready" while the world is loading | patch P4 |
| 5 | Password hashing and verification run on the event loop (`main.py` login_submit, setup_submit) | medium | Confirmed: both call into bcrypt/argon2 directly from `async def`, holding the GIL for hundreds of milliseconds and stalling every other request including every open log pump. Every Docker call in the same file is correctly wrapped in `run_in_threadpool` — these two are the inconsistency | patch P5 |
| 6 | `force = bool(...)` treats any truthy JSON value as force (`main.py:623`) | medium | Confirmed: `bool("false")` is `True`, so `{"force": "false"}` SIGKILLs a running server instead of stopping it gracefully — a world save can be lost. One-line fix | patch P6 |
| 7 | The documented verification command cannot pass (`README.md`, `tests` `project_root`, `Dockerfile`) | medium | Confirmed by arithmetic: the fixture uses `parents[3]`; in the image the test file is `/srv/app/tests/...`, so `parents[3]` is `/`. The Dockerfile copies only `requirements.txt`, `app` and `tools`, and `valheim.env.example` lives at the repo root outside the build context — two guaranteed `FileNotFoundError`s. So `docker compose exec manager python -m pytest` is red on a clean tree, and the `DEFAULT_SETTINGS_TEXT` drift check never runs where it is documented to run | patch P7 |
| 8 | `STATUS_EVERY_N_POLLS` is a dead knob in the real stack | medium | Confirmed: present in `manager/.env.example:128` and read at `main.py:187`, but **absent from `docker-compose.yml`**, unlike every sibling knob. Setting it in `.env` silently does nothing, and its test monkeypatches the environment directly so it gives false confidence | patch P8 |
| 9 | The error banner never clears on recovery (`app.js:clearError`) | medium | Confirmed: `clearError()` is reachable only from `post()`, so a transient `log_error`/`settings_error`/`status.error` paints the banner and leaves it there — an operator who never presses a button sees a stale red banner beside a green "ready" badge | patch P9 |
| 10 | The healthcheck rationale is factually wrong in three places (`docker-compose.yml`, `main.py` `_UNCONFIGURED_ALLOWED`, `README.md`) | low | Correct: Docker Engine and Compose act on process exit, never on health status — only Swarm or an autoheal sidecar restarts an unhealthy container. Exempting `/healthz` from the setup gate is still right; the stated reason is not, and the next reader inherits the misconception. Direct correction | patch P10 |
| 11 | `mkstemp` failure escapes as a raw 500 in the wizard (`state_store.py:230`) | low | Real: `ensure_writable` passing does not prevent ENOSPC or a remount, and the resulting `OSError` bypasses `StateStoreError`, so the operator gets a 500 naming neither path nor cause | patch P11 |
| 12 | Five surfaces where deleting real behaviour leaves the suite green | medium (pre-verified) | Filed: (a) the production controller wiring is never executed — all 19 `create_app` calls inject a controller and the test helper re-implements the port math, so changing `_udp_ports` to drop the query/crossplay ports passes while breaking joins; (b) `config_from_env`'s non-numeric reads (`ALLOWED_ORIGINS`, the credential trio, `COOKIE_SECURE`, `MANAGER_URL`) are never set via the environment in any test, so a rename locks reverse-proxy operators out undetected; (c) no test provokes a failure *inside* `commit()`, so swapping its two writes ships a half-completed install on the exact mount failure the prepare/commit split exists for; (d) settings-file mode preservation is unasserted, so dropping the `chmod` silently makes the operator's file `0600`; (e) `/login`'s origin check has no negative test, unlike its three siblings | patch P12 |
| 13 | Duplicate log lines during the first-run burst (`main.py` `_pump`, `docker_control.fetch_logs`) | low | Real: `since` is truncated to whole seconds so each poll re-reads the final second, relying on a fixed 2000-entry dedupe deque; SteamCMD progress can exceed that within one second, evicting entries before the re-read matches them — at exactly the moment the README tells the operator to watch the console | patch P13 |
| 14 | Neither atomic writer fsyncs the parent directory after `os.replace` | low | Correct: contents are fsynced and the rename is atomic, but the rename itself can be lost in a host crash — which for the credential file means the admin account vanishes and the wizard reopens, the outcome `state_store`'s own docstring says must never happen. Either fsync the directory or soften the claim | patch P14 |
| 15 | No `cap_drop`/`no-new-privileges` on the manager or the proxy | low | Real and now more pointed: the manager has a first-boot admin-creation path and create-container rights via the proxy. Previously rejected as beyond scope; two compose lines that cannot break anything are worth taking. `read_only`/`pids_limit` remain out (the manager writes state) | patch P15 |
| 16 | The README's `MANAGER_UID` remedy omits deleting the state volume | low | Real: the named volume keeps its original ownership, so following the note alone leaves `ensure_writable` failing and the manager crash-looping | patch P16 |
| 17 | `deferred-work.md` referenced but "not in the tree" | **false** | The file exists at `_bmad-output/implementation-artifacts/deferred-work.md` and predates this build. The reviewed diff deliberately excludes `_bmad-output/**`, so its absence there proves nothing — the identical false claim was raised and refuted in the previous build's review | rejected |
| 18 | bcrypt truncates the password at a NUL byte, so a shorter prefix also unlocks | **false** | Measured on the pinned bcrypt 4.2.1: a password containing a NUL hashes normally and the pre-NUL prefix does **not** verify against it. No truncation, no weakening | rejected |
| 19 | A container paused outside the manager has no recovery path | **false** | `stop()` accepts `paused` and the engine stops/kills paused containers, so Stop is the way out; carried refutation from the previous build's review, where the same claim was checked | rejected |
| 20 | `/ws/logs` performs no Origin check | low | Real, but pre-existing in the previous build rather than caused by this change, and `SameSite=Strict` is the actual barrier (browsers apply it to WebSocket handshakes). Worth doing as defence in depth, not here | defer |
| 21 | The socket-proxy allowlist is exercised by no test | low (pre-verified) | Filed with disposition `defer`: tightening `ALLOW_STOP`/`ALLOW_RESTARTS` would pass the whole suite while breaking Stop and Force stop, but closing it needs a live daemon plus the proxy container, which this suite deliberately avoids | defer |
| 22 | `app.js` hardcodes the ready regex and ignores the `ready_pattern` it is sent | low | Confirmed again, and raised by two layers. Consequence is still only a missing highlight colour after a rare `READY_LOG_PATTERN` override, and the fix needs a try/catch around an operator-supplied regex. Carried rejection from the previous review | rejected (low) |
| 23 | Every Docker-layer failure becomes HTTP 502, including "no container to stop" | low | Real API-honesty issue; no user-visible harm beyond a mislabelled status code, and the UI shows the message either way | rejected (low) |
| 24 | No CSP / `X-Content-Type-Options` / `Cache-Control: no-store` | low | Log lines are rendered with `textContent`, so the console is not an injection sink; carried rejection from the previous review | rejected (low) |
| 25 | `requests` is an undeclared direct test dependency | low | True but transitive via the pinned `docker==7.1.0`; the related "test deps in the runtime image" half is folded into P7 | rejected (low) |
| 26 | `hash_password` builds a fresh `PasswordHasher`; `_ready_runs` grows unbounded | low | Both confirmed and both cosmetic — one entry per container run per process, and `set.add` is atomic under the GIL. Carried rejection | rejected (low) |
| 27 | A hand-edited invalid `SERVER_PORT` publishes 2456-2458 while the container gets the bad value | low | Real but requires hand-editing an invalid port, and `server_port()` already warns and falls back; the mismatch is visible in the logs | rejected (low) |
| 28 | `VALHEIM_PORTS` can drift from `SERVER_PORT` on the `--profile server` path | low | Already a documented drift in the previous build, and that path is an explicit bypass | rejected (low) |
| 29 | A backslash-escaped quote inside a quoted value is truncated | low | Obscure hand-edit shape; Compose's own parser disagrees with several such forms too | rejected (low) |

### Outcome of the patch pass

All 16 patch entries applied and verified here independently of the implementer's report.
**204 passed, 2 skipped** (170 before review). What I checked directly rather than accepting:

- Re-measured the two defects I had proven by experiment. The non-UTF-8 settings file now
  raises `SettingsFileError` in all three shapes I had made fail (latin-1 `read`, latin-1
  `write`, UTF-16 `read`), and an empty or 4-character join password is now refused while 5
  is accepted — so the zero-effort path can no longer produce a server that refuses to boot.
- Mutation-tested three fixes: re-allowing an empty join password, reverting `force` to a
  truthy `bool()`, and disabling the readiness rescan throttle. Each was caught by a named
  test, and the suite returned to 204 after every restore.
- Confirmed the compose hardening actually renders (`cap_drop`, `no-new-privileges` on both
  services, the `json-file` size cap on the profile-gated `valheim` service, and
  `STATUS_EVERY_N_POLLS` now passed through), and that `docker compose config -q` is still
  valid with no env files present.
- Checked the one test the implementer changed rather than trusting its rationale:
  `test_readiness_line_flips_running_to_ready` keeps both original assertions and adapts only
  its *setup* (advancing the monotonic clock past the new throttle), with a new companion test
  pinning that the log-pump path still flips readiness without waiting. The expectation was
  not edited to match the code.

Added by the reviewer, not the implementer: a README troubleshooting section for
`cap_drop: [ALL]` on the socket proxy. That flag is asserted safe on the grounds that haproxy
binds an unprivileged port, but it was validated only through `docker compose config` — no
Docker daemon was available at any point in this build — and some haproxy images drop
privileges internally and need `CAP_SETUID`/`CAP_SETGID`. It is the change most likely to stop
the stack on a first real run, so the remedy is now documented next to the manual pass.

**Unverified surface remains the same as the previous build:** no `docker compose build` or
`up -d --build` has ever run, so the Dockerfile, the new capability and logging keys, a real
image pull, the readiness line against live server stdout, and a game-client connection are
all still untested against an engine.

## Design Notes

The one-time token is the deliberate compromise on "simplest possible install": an unauthenticated admin-creation page would let whoever loads it first own a root-equivalent service on the LAN. So install is `docker compose up -d` plus one URL from `docker compose logs manager` — the Jupyter/Portainer precedent. Chosen by the reviewer after the human declined to pick; an open or time-limited wizard is a contained swap.

Moving credentials into a state file also retires the `$`-interpolation hazard the previous build fought (`format: raw`, quoting, silently truncated hashes): with no hand-edited secret file, nothing is left for Compose to mangle. Mounting a settings directory rather than a file likewise retires the missing-file-becomes-a-directory trap.

## Verification

**Commands:**
- `docker compose config -q` -- expected: valid with no env files present
- `python -m pytest` -- expected: passes, including the new setup and state cases
- `docker compose up -d --build` -- expected: boots unconfigured and logs a setup URL

**Manual checks (if no CLI):**
- From a clean tree, complete the wizard in a browser; confirm the state file is `0600`, `/setup` is closed, and the logged token no longer works
- Restart the manager: session survives, setup stays closed
- Confirm no game container or world exists until Start is pressed
