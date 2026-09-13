---
title: 'Valheim Server Docker Stack with Live Log Console WebUI'
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

**Problem:** The user wants to self-host a Valheim dedicated server in Docker, but has no way to run/stop it or watch its logs without hand-typing `docker` commands against an image such as `ghcr.io/community-valheim-tools/valheim-server-docker`.

**Approach:** Ship a docker-compose stack pairing that unmodified image with a login-gated manager service whose WebUI shows real server status, offers start/stop/restart, and streams the server's stdout into a live log console. In-UI settings editing and a manual backup button are deferred (see `deferred-work.md`).

## Boundaries & Constraints

**Always:**
- The game server runs as the unmodified `ghcr.io/community-valheim-tools/valheim-server-docker` image, configured only via its documented env vars/volumes.
- The manager reaches the Docker Engine API only through a scoped `docker-socket-proxy` sidecar allowlisting just the endpoints used (container inspect/start/stop/create/remove, logs) — never the raw host socket, never the `docker` CLI.
- Status distinguishes *container running* from *server ready*: readiness comes from the server's own stdout listening line, and the UI shows both states distinctly.
- Login is required: a single admin account (`ADMIN_USER` + `ADMIN_PASSWORD_HASH`, bcrypt or argon2 — never a bare SHA) behind a `SameSite=Strict` cookie signed with a stable `SESSION_SECRET`, so manager restarts don't silently log the operator out.
- Every state-changing endpoint (start/stop/restart) requires an authenticated session *and* an origin check.
- Server settings are read from an env file the compose stack feeds to the `valheim` service; the manager reads it to display current values but does not write it in this build.

**Never:**
- No settings editing, no "Backup now" button, no mod toggles, no admin/ban/permit-list editor, no backup browse/restore — all deferred (`deferred-work.md`).
- No in-game admin commands (kick/ban).
- No public-internet exposure by default; docs must state it binds LAN-only unless the operator deliberately opens it up.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| First run | No Valheim container exists, operator clicks Start | Image pulled if absent, container created from the env file, started; UI reports pulling → creating → starting distinctly | Pull/create failure surfaces the Docker error and leaves no half-created container |
| Start server | Container exists but stopped, operator clicks Start | Container starts; UI shows starting → running → ready | Start failure shown as an error banner carrying the Docker error text |
| Server becomes ready | Container running, world finishes loading | Readiness line on stdout flips UI from "running" to "ready" | If readiness never arrives, UI stays "running (not yet ready)" rather than claiming ready |
| Stop server | Container running, operator clicks Stop | Graceful stop (SIGTERM, timeout, then kill) | Timeout shown with a force-stop option |
| Live log streaming | Container running, WebUI open | Log lines append near-real-time, auto-scrolling | On WebSocket drop, auto-reconnect and backfill last ~200 lines |
| Unauthenticated access | Anyone loads the WebUI without logging in | Redirected to login; no status, controls, or logs exposed | Wrong credentials show a generic error, no user enumeration |
| Cross-site control attempt | State-changing request with a session cookie but foreign/absent origin | Rejected before any Docker action | Returns an error; nothing on the container changes |

</frozen-after-approval>

## Code Map

- Reference image `ghcr.io/community-valheim-tools/valheim-server-docker` (greenfield project — no existing code) -- env vars `SERVER_NAME`/`SERVER_PORT`/`WORLD_NAME`/`SERVER_PASS`/`SERVER_PUBLIC`/`CROSSPLAY` configure the server; game ports 2456-2457/udp; it logs only to stdout (`docker logs`); its `/config` volume holds worlds, lists, and `backups/`. The readiness signal to watch on stdout is the server's own "game server connected"/listening line — confirm the exact wording from live logs during implementation rather than guessing.
- Socket proxy -- an established scoped proxy image (e.g. `tecnativa/docker-socket-proxy`) with only the needed endpoint permissions enabled; the only service mounting `/var/run/docker.sock`.

## Tasks & Acceptance

**Execution:**
- [x] `docker-compose.yml` -- define `valheim`, `manager`, and the `docker-socket-proxy` sidecar; `manager` talks only to the proxy, `valheim` gets its env from the settings env file -- makes the whole stack one `docker compose up -d`
- [x] `manager/Dockerfile` -- small Python image for the manager backend + WebUI
- [x] `manager/app/settings_store.py` -- read and parse the Valheim env file for display (read-only in this build)
- [x] `manager/app/docker_control.py` -- Docker SDK wrapper pointed at the proxy: inspect/start/stop, pull-and-create on first run, stream logs, detect the readiness line
- [x] `manager/app/auth.py` -- session-cookie login: `ADMIN_USER`/`ADMIN_PASSWORD_HASH` (bcrypt or argon2), `SESSION_SECRET`-signed `SameSite=Strict` cookie
- [x] `manager/app/main.py` -- FastAPI app: login route, status/start/stop/restart routes behind auth + origin check, `/ws/logs` WebSocket
- [x] `manager/app/templates/` + `manager/app/static/app.js` -- WebUI: status panel (stopped/starting/running/ready), controls, current-settings display, live log console
- [x] `manager/tools/hash_password.py` -- one-shot helper so the operator can generate a valid `ADMIN_PASSWORD_HASH`
- [x] `manager/app/tests/test_edge_cases.py` -- unit-test the I/O matrix edge cases: first-run create path, readiness-never-arrives state, foreign-origin rejection, unauthenticated access
- [x] `manager/.env.example` -- documents every configurable env var for all three services
- [x] `README.md` -- `docker compose up -d` quick start, how to generate the password hash, security notes (proxy-scoped Docker access, LAN-only default)
- [x] `valheim.env.example` -- added: the settings env file `.env.example` points at; `env_file` must not carry the manager's secrets, so the two files are separate
- [x] `manager.env.example` -- added during review: the manager's three credentials, loaded with `format: raw` so Compose's `$` interpolation cannot truncate the password hash

**Acceptance Criteria:**
- Given the stack is started with `docker compose up -d`, when the operator opens the manager URL without logging in, then they see only a login screen with no status, controls, or logs.
- Given no Valheim container exists yet, when the operator clicks Start, then the image is pulled if needed, the container is created and started, and the UI reflects each phase.
- Given the container starts, when its stdout reports it is listening, then the UI moves from "running" to "ready"; until then it never claims ready.
- Given the server is running, when the operator watches the console, then new log lines appear within a couple of seconds of being emitted.
- Given the manager container is restarted, when the operator reloads the page, then their session is still valid.
- Given the WebSocket log stream drops, when the connection returns, then the console reconnects and resumes without a page reload.

## Implementation Notes

**Game container ownership.** The `valheim` service sits behind a `server` Compose
profile, so `docker compose up -d` starts only `manager` + `docker-socket-proxy` and
the manager creates the game container itself. Without this, Compose would create and
start the container before the operator ever pressed Start, and the spec's first-run
phase reporting (pulling → creating → starting) would have nothing to report. The
compose service definition remains as the canonical container shape and as a bypass
(`docker compose --profile server up -d valheim`); README documents the one place the
two can drift (published ports: manager derives them from `SERVER_PORT`, the profile
path uses `VALHEIM_PORTS`).

**Log transport.** Logs are polled (`docker logs --since --timestamps`, default 1 s)
rather than followed. A followed stream from the SDK blocks in a thread that cannot be
cancelled when a browser tab closes; polling keeps every read cancellable, makes the
"backfill last ~200 lines on reconnect" behaviour fall out of the same code path, and
still lands lines within the "couple of seconds" the acceptance criteria ask for.
Engine timestamps double as dedupe keys across the 1-second `since` overlap.

**Readiness cache.** Keyed by `(container id, State.StartedAt)`, so a restart drops
back to "running (not yet ready)" automatically. A status call with nobody watching the
UI scans the current run's recent output once; a readiness scan that *fails* never
marks ready.

**Origin check** is strict — `Origin` must be present and match the request host (or
`ALLOWED_ORIGINS`). No `Referer` fallback, because the I/O matrix requires *absent*
origin to be rejected too. Applied to `/login` and `/logout` as well as the three
control endpoints.

**Startup validation.** `SessionAuth` raises on a missing/short `SESSION_SECRET`, a
missing `ADMIN_USER`, or a hash that is not bcrypt/argon2 (a bare SHA/MD5 digest is
detected and named explicitly), so a misconfigured manager fails at boot rather than at
login. The Compose `${VAR:?message}` guards that previously backed this up are gone —
the three credentials now arrive via `env_file: format: raw`, which cannot carry such a
guard — so this startup validation is the only thing standing between a misconfigured
credential and a confusing login failure. A missing `manager.env` is reported by name by
Compose itself.

**`.gitignore` inline comments (found in verification).** The ignore patterns had been
written as `.env  # Compose knobs`. Git only treats `#` as a comment at the *start* of a
line, so each pattern was the literal text including the comment and matched nothing —
`git add -A` staged `.env`, `manager.env`, and `valheim.env`, i.e. the admin hash,
session secret, and server password. Comments moved to their own lines; verified with
`git check-ignore` in a throwaway repo that all three are now ignored and only
`.gitignore` stages.

## Spec Change Log

- **Image name corrected.** The spec names `ghcr.io/community-valheim-tools/valheim-server-docker`;
  that is the *repository* name. The published package is
  `ghcr.io/community-valheim-tools/valheim-server` (verified on the project's GHCR
  package page and its own `docker-compose.yaml`). Used the real name, overridable via
  `VALHEIM_IMAGE`. Same upstream project, same unmodified image — intent unchanged.
- **Readiness line confirmed, not guessed.** The Code Map asked for the exact wording to
  be confirmed from live logs. The Docker daemon was unavailable in this environment, so
  it was confirmed from the image's own documentation instead: `Game server connected`.
  The matcher is a case-insensitive regex (`game server connected|Ready for connections`)
  and is overridable via `READY_LOG_PATTERN`, so a game update that changes the wording is
  a config edit, not a code change. **Still needs one live-log confirmation** — see
  Verification.
- **Extra file: `manager.env.example` (review fix).** Docker Compose interpolates `$`
  in `.env` *and* in a default-format `env_file:`. Every bcrypt (`$2b$12$…`) and argon2
  (`$argon2id$v=19$…`) hash contains `$`, so the value is silently truncated — bcrypt to
  `$2b$12`, argon2 to an unrecognisable fragment — and login then fails forever with only
  "Invalid username or password". Measured on Compose v2.38.1. The three manager
  credentials moved to `manager.env`, loaded with `env_file: [{path, format: raw}]`, the
  only loader that passes the value through untouched (requires Compose **v2.30.0+**,
  now stated in the README and the compose header). Raw format also takes quotes
  literally, so values there must be bare — the opposite of the `.env` rule, called out
  in both example files. Added regression tests; `validate_password_hash` now checks the
  hash's full structure and names this cause when it sees the wreckage.
- **Extra file: `valheim.env.example`.** The task list named only `manager/.env.example`.
  Compose `env_file:` injects every variable in the file into the target container, so a
  single combined file would have leaked `ADMIN_PASSWORD_HASH` and `SESSION_SECRET` into
  the game container. Split: `.env` (Compose interpolation + manager) and `valheim.env`
  (game settings only). `manager/.env.example` still documents every variable for all
  three services, as specified.

## Review Triage Log

Pass 1 (`review_loop_iteration: 0`). Three layers ran: `blind-hunter` (22), `edge-case-hunter`
(19), `verification-gap` (13). No `intent_gap` and no `bad_spec` entries, so no loopback.
Verification-gap rows arrive pre-verified per the layer's evidence rules. Scope note: the
frozen block defers in-UI settings editing and the "Backup now" button, so their absence is
spec-conformant, not a deviation — several reviewer remarks that assumed otherwise are
refuted on that basis.

| # | Finding (location) | Verdict | Evidence | Route |
|---|---|---|---|---|
| 1 | Force-stop button re-hides ~2 s after appearing (`app.js:98`) | high | Traced: 502 sets `force.hidden=false`; next WS push has `pendingAction=false` and phase still `running`, so `!busy && phase!=="stopping"` is true and hides it. Defeats frozen matrix row "Timeout shown with a force-stop option" | patch P1 |
| 2 | Controls stay disabled after a rejected/failed action (`app.js:213-221`) | medium | `renderStatus` runs while `pendingAction` is still true (disabling all three); `pendingAction=false` at :219 never re-renders. `.catch` at :216 returns no payload, so if the manager is unreachable the WS is too — buttons stay dead until reload | patch P2 |
| 3 | `post()` leaves buttons disabled until next push (verification-gap, other) | medium | Same defect as #2 — grouped | patch P2 |
| 4 | Non-Docker engine failures escape as 500 (`docker_control.py:399,443`, get_container, fetch_logs) | medium | `requests.ReadTimeout` is not a `DockerException`; `_run_action` catches only `DockerControlError`. `stop()` at :435 already has a bare `except Exception` commented "read timeout from the HTTP client" — author knew, handled one site only. In `_pump`, `safe_status` misses it too and the pump task dies | patch P3 |
| 5 | `container.reload()` raising escapes raw (`:399`, `:443`) | low | Real but uncommon; same root cause as #4 — grouped into P3 rather than rejected | patch P3 |
| 6 | Readiness scan capped at `tail=400` (`docker_control.py:242-248`) | medium | `_ready_runs` is in-process; after a manager restart on a long-running server the ready line is >400 lines back, so `_is_ready` is permanently False and `fetch_logs` only sees new lines. Badge sticks at "running (not yet ready)" — contradicts the frozen AC "when its stdout reports it is listening, then the UI moves to ready" | patch P4 |
| 7 | `LOG_POLL_SECONDS=0` busy-loops (`main.py:106,385`) | medium | `_env_float` passes "0" through (non-empty string is truthy); `asyncio.sleep(0)` per client, unclamped — while the adjacent `status_every_n_polls` *is* guarded with `max(1,...)` | patch P5 |
| 8 | `SESSION_MAX_AGE_SECONDS=0` → endless login redirect (`main.py:111`) | medium | Same unvalidated-numeric root cause; cookie expires instantly, no diagnostic, unlike every other auth misconfig which fails loudly | patch P5 |
| 9 | `LOG_TAIL_LINES` negative / `SERVER_PORT` out of range (`main.py:105`, `settings_store.py:109`) | low | Confirmed unbounded; fails at the engine with a loud error. Folded into P5 as the same class | patch P5 |
| 10 | `_split_image_ref` mangles digest-pinned refs (`docker_control.py:83-88`) | medium | `rpartition(":")` on `repo@sha256:abc` yields `("repo@sha256","abc")`; `VALHEIM_IMAGE` is a documented knob, so a pinned digest makes first Start always fail | patch P6 |
| 11 | Fixed 900 s `_Action` expiry outlived by a first-run pull (`docker_control.py:77`) | medium | ~1 GB pull on a slow link exceeds 15 min; `_current_action` then clears, `status()` reports `absent`/"No server container yet.", UI re-enables Start mid-pull → duplicate create 409. The `api_timeout` half of the claim is not established (pull progress frames refresh the read timeout) | patch P7 |
| 12 | README "Changing settings" procedure does not work (`README.md:174-185`) | medium | `docker compose stop valheim` cannot resolve a `server`-profile service without `--profile` (error hidden by `2>/dev/null`), and `docker rm` refuses a running container. Removal is load-bearing because `start()` only creates when absent — so the documented path silently leaves old settings live. Also hardcodes `valheim-server` over `${VALHEIM_CONTAINER_NAME}`, and claims "the manager recreates the container from the new file", which it does not | patch P8 |
| 13 | "LAN-only by default" false on a public-IP host (`README.md:138-140`, `docker-compose.yml:123-124`) | medium | `MANAGER_BIND=0.0.0.0` on a VPS is immediately internet-facing; README asserts "reachable from your LAN and nowhere else… only if you deliberately forward the port". Security-relevant false assurance for a login with no rate limiting | patch P9 |
| 14 | Missing `valheim.env` becomes a host directory (`docker-compose.yml:129`) | medium | Bind mount with absent source → daemon creates a *directory*; `docker compose up -d` excludes the profiled `valheim` service so its loud `env_file` check never fires; the obvious `cp valheim.env.example valheim.env` then writes *inside* that directory | patch P10 |
| 15 | `deferred-work.md` referenced but never created (spec Intent/Boundaries, `README.md:185`, `settings_store.py:9`, `valheim.env.example`) | medium | File absent from the tree; four places point at it. This step must append defer entries there regardless | patch P11 |
| 16 | Settings panel `innerHTML`-rebuilt every ~2 s (`app.js:103-116`) | low | Confirmed full wipe+rebuild per status push, destroying any text selection in the panel the operator reads values from. Kept because the fix is a 2-line signature guard, not added complexity | patch P12 |
| 17 | `start()` has two `except` blocks with identical bodies (`docker_control.py:402-407`) | low | `DockerControlError` is an `Exception`; deleting the first block preserves behaviour exactly — a pure deletion | patch P13 |
| 18 | `hash_password.py` argon2 truncation warning contradicts tested reality (`tools/hash_password.py:71`) | low | Prints `$argon2id` as the truncation, but the project's own regression constant `MANGLED_ARGON2 = "=19=65536,t=3,p=4+b+dWRWJTmaaJObG"` (tests:705) proves the prefix vanishes entirely. A guard rail stating something its own suite disproves | patch P14 |
| 19 | `status_every_n_polls` is a dead knob (`main.py:83,349`) | low | Declared on `AppConfig` and consumed in `_pump`, but `config_from_env()` reads no env var for it, unlike every other tunable. Direct correction | patch P16 |
| 20 | `restart()` never executed by any test | medium (pre-verified) | Filed: only `restart_policy` plumbing and two 401/403 rejections touch `/api/restart`; dropping `return self.start()` keeps the suite green while Restart leaves the server down | patch P15 |
| 21 | `pulling`/`creating`/`starting` phases never observed | medium (pre-verified) | Filed: all 11 `["phase"]` assertions expect only absent/running/ready/stopped/error; deleting every `_set_action` call passes. This is the behaviour the manager-owned-container design exists for | patch P15 |
| 22 | Force stop only exercised below the HTTP route | medium (pre-verified) | Filed: `control.stop(force=True)` called directly; `main.py:300` body parsing never executed. Pairs with #1 — the same escape hatch is broken in the UI and unverified in the suite | patch P15 |
| 23 | `/logout` never requested by any test | medium (pre-verified) | Filed: zero `logout` matches in the suite; dropping `path="/"` would silently break sign-out | patch P15 |
| 24 | Session expiry unpinned | medium (pre-verified) | Filed: no `max_age`/expiry test; removing `max_age=` from `loads()` makes cookies eternal with a green suite | patch P15 |
| 25 | `COOKIE_SECURE` never asserted | medium (pre-verified) | Filed: only cookie test runs with the flag off; deleting `"secure"` from `cookie_kwargs()` passes, shipping a non-Secure cookie in the documented TLS deployment | patch P15 |
| 26 | `/healthz` untested | low (pre-verified) | Filed: backs the compose healthcheck; no request to it anywhere | patch P15 |
| 27 | Create-call contract asserted for half its keys | medium (pre-verified) | Filed: `restart_policy`, `network`, `cap_add`, `stop_timeout`, `labels` never asserted; dropping `restart_policy` means no restart after host reboot, suite green | patch P15 |
| 28 | Phase mapper's non-running states never reached | medium (pre-verified) | Filed: fake only holds running/exited/absent, so `restarting`/`paused`/`removing`/`created` branches never execute; a crash-looping server would report "stopped" with Start enabled | patch P15 |
| 29 | Test coverage gaps (blind-hunter #16) | — | Duplicate of #20-28 plus `_split_image_ref`/`config_from_env` helpers; merged into P15 | patch P15 |
| 30 | Code pulls `valheim-server`, spec names `valheim-server-docker` | **false** | Registry check: `ghcr.io/community-valheim-tools/valheim-server:latest` → HTTP 200; the `-docker` form returns no pull token (does not exist). The spec's Code Map names the *GitHub repo*; the code's image ref is the correct published package | rejected |
| 31 | `ALLOW_PAUSE`/`ALLOW_UNPAUSE` unsupported by the proxy | **false** | Upstream Tecnativa README lists both as supported granular controls (revoked by default), so the compose comment is accurate and the endpoints are genuinely blocked | rejected |
| 32 | Paused container has no path out; `start()` 409s | **false** | `stop()` accepts `paused` (`:414`) and Docker stops paused containers, so Stop is the way out; Start's 409 surfaces as a banner with the engine's text — a loud failure on a state only reachable by bypassing the manager (the proxy revokes pause) | rejected |
| 33 | Origin allowlist derived from the request's own `Host` (DNS rebinding) | low | Real (`main.py:196-200`), but `SameSite=Strict` is the actual cross-site guard; fix means mandating explicit `ALLOWED_ORIGINS`, breaking the zero-config LAN default | rejected (low) |
| 34 | Absent `Origin` blocks same-origin form login | low | Modern browsers send `Origin` on form POSTs; the frozen matrix explicitly requires absent-origin rejection, and a `Referer` fallback would weaken it | rejected (low) |
| 35 | Concurrent Start/Stop/Restart race | low | No action lock, but the UI disables buttons per tab and a cross-tab collision yields a loud 409 name conflict with no corruption; fix adds locking | rejected (low) |
| 36 | Readiness re-scanned with no negative caching | low | Confirmed one extra `logs(tail=400)` read per ~2 s while loading; perf only, and `fetch_logs` already caches on the positive path. Fix adds TTL state | rejected (low) |
| 37 | `_remove_container` swallows cleanup errors | low | Best-effort cleanup logs a warning while the original `DockerControlError` still reaches the operator with the engine text; raising here would mask the real failure | rejected (low) |
| 38 | Invalid `READY_LOG_PATTERN` → `re.error` traceback | low | Fails loudly at startup, just without a friendly message; a typo in an optional override. Fix adds a guard | rejected (low) |
| 39 | `MANAGED_LABEL` written, never read | low | Cosmetic; `_remove_container` is reached only when `created_here` is true, so nothing acts destructively on an unowned container | rejected (low) |
| 40 | Client-side ready regex ignores `status.ready_pattern` | low | Confirmed duplicate literal at `app.js:161`; consequence is a missing highlight colour after a rare override, and the fix needs a try/catch for operator-supplied regexes | rejected (low) |
| 41 | `hash_password` ignores `rounds` for argon2 | low | Confirmed (`auth.py:146`), also builds a second `PasswordHasher`; nobody passes `--rounds` with argon2 and the result is still a valid hash | rejected (low) |
| 42 | `stop()`'s bare `except Exception` mislabels programming errors | low | Real, but that block is precisely what maps HTTP read timeouts to a force-stop offer; narrowing it risks the path it exists for | rejected (low) |
| 43 | No `Cache-Control: no-store`/CSP/`X-Frame-Options`; manager lacks `cap_drop`/`no-new-privileges`/`read_only` | low | Hardening beyond the frozen scope's stated controls (login + origin + scoped proxy); adds compose surface | rejected (low) |

### Pass 1, addendum — rows missed on the first assembly

Six findings were analysed during triage but dropped when the table above was written, and
one row asserted something false. Recorded here with the same verification standard; the
surviving ones were dispatched as a second patch batch (P17-P21).

| # | Finding (location) | Verdict | Evidence | Route |
|---|---|---|---|---|
| 15c | **Correction to row 15** — `deferred-work.md` was reported as "absent from the tree". That is **false** | — | The file exists at `_bmad-output/implementation-artifacts/deferred-work.md` and predates this review pass. The diff handed to the reviewers excluded `_bmad-output/**`, so its absence there proved nothing, and the row repeated that inference without checking the path. The implementer split its 3 entries into 5 itemized ones, preserving the original evidence | corrected; no fix needed |
| 44 | `valheim.env.example:31,35` ships `SERVER_PASS=changeme123` together with `SERVER_PUBLIC=1` | medium | Confirmed both lines. The password satisfies the image's ≥5-character rule, so an operator who copies the example and skips the edit gets a server **listed in the public browser** with a join password published in this repo. The settings panel masks it, so the UI gives no hint it is still the default | patch P17 |
| 45 | WebSocket session is authorised once at the handshake and never re-checked (`main.py:371` vs the `while True` pumps at `:389,:397`) | medium | Confirmed by reading both loops: no further `read_token`. A session that expires, or an operator who signs out in another tab, keeps receiving live status and log lines until the socket happens to drop — with a 7-day default lifetime. Makes the README's "an unauthenticated client never receives a log line" false for a client whose session has ended. The JS already redirects on close code 1008, so that branch is currently dead for this case | patch P18 |
| 46 | bcrypt silently truncates passwords at 72 bytes (`auth.py` `hash_password`/`verify_password`) | medium | Measured on the pinned bcrypt 4.2.1: hashing an 80-character password and then verifying a *different* 81-character password that diverges only after byte 72 returns **True**. So any passphrase beyond 72 bytes has its tail silently ignored — the earlier triage assumption that bcrypt raises was wrong | patch P19 |
| 47 | Editing `valheim.env` then pressing Start/Restart silently keeps the old settings (`docker_control.py` `start()` only creates when the container is absent) | medium | Confirmed: `start()` returns early for a running container and otherwise starts the existing one, whose env was baked at create time. Spec-conformant (the frozen block defers settings *writing*), but the panel displays file values while the container runs different ones, with nothing saying so. Row 12's README fix supplies the correct procedure; the UI still needs to point at it | patch P20 |
| 48 | `POST /logout` clears the cookie but the signed token stays valid until `max_age` (`main.py:275-277`) | medium | Confirmed stateless `itsdangerous` token with no server-side revocation, so a copied cookie survives sign-out for up to 7 days. A real epoch would need to persist across restarts to be meaningful, which is more than this build's scope; the honest fix is to state it beside the other accepted trade-offs | patch P21 (document) |
| 49 | `COOKIE_SECURE=true` while served over plain HTTP silently loses the cookie (`main.py:112`) | low | Real — the browser drops a `Secure` cookie on an http origin, giving a login loop with no diagnostic. But it is a misconfiguration the README already guards by telling the operator to set it only behind TLS, and the fix adds a scheme-sniffing branch | rejected (low) |

### Outcome of the patch passes

All 21 patch entries were applied across two batches and verified here independently of the
implementer's report: every changed file diffed against the pre-patch tree (scope was exactly
the files dispatched, nothing else touched), exception-handler ordering re-checked so
`NotFound` still precedes the broadened catches, and four fixes mutation-tested by hand —
re-capping the readiness scan, neutering `restart()`'s trailing start, disabling the
WebSocket re-check, and raising `BCRYPT_MAX_BYTES` — each caught by named tests, with the
suite returning to green after every restore. **98 tests pass** (41 before review).

Two corrections the implementer made to this review were checked and upheld: `deferred-work.md`
already existed (row 15c), and dropping `path="/"` from `delete_cookie` is a no-op because
Starlette already defaults to it, so that particular mutation proved nothing — though the
underlying gap (no logout test at all) was real and is now covered.

One scope limit worth recording against row 45: `websocket.cookies` is captured at the
handshake and never refreshed by the browser, so re-validating it catches session **expiry**
but cannot observe a sign-out in another tab — that needs the server-side revocation row 48
accepts as out of scope. Both the code comment and the README state this rather than leaving
it implied.

Test-environment note, not a product defect: `test_hash_password_tool_reports_the_limit_instead_of_a_traceback`
spawns `sys.executable`, which fails when the interpreter lives under a path long enough that
Python reports it with a `\\?\` prefix (Windows `subprocess` cannot execute that form). The
suite is green in a normal-path environment; left unchanged rather than adding path
normalisation for a pathological case.

## Design Notes

Backend: FastAPI + the `docker` Python SDK pointed at the proxy (`DOCKER_HOST=tcp://docker-socket-proxy:2375`), not `from_env()`. Frontend: server-rendered HTML + vanilla JS (no build step) — a WebSocket client appends log lines to a scrolling `<pre>` console and receives status pushes driving the control buttons' enabled state.

Accepted trade-offs, deliberately not built: no login rate-limiting or lockout (single-operator LAN tool); each browser tab opens its own Docker log stream, fine at this scale; the stack assumes Linux containers under Docker Desktop/WSL2 or a native Linux host — Windows containers/Hyper-V mode would need rework.

## Verification

**Commands:**
- `docker compose config` -- expected: valid merged configuration, no errors
- `docker compose up -d --build` -- expected: all three containers start; manager reachable on its published port
- `docker compose exec manager python -m pytest` -- expected: the edge-case suite passes

**Manual checks (if no CLI):**
- Confirm the login gate blocks controls/logs before signing in, then log in
- Run a full first-run create → start → ready → stop → start cycle and confirm each UI state matches the container's real state
- Watch the console during startup and confirm it shows the same lines as `docker compose logs -f valheim`
- Connect a Valheim client to the advertised address once the UI reports "ready"

**Status of verification (2026-09-12).** The Docker daemon was not running in this
environment, so `docker compose up -d --build` and the in-container pytest run could not
be executed. What *was* run:

- `docker compose config -q` and `docker compose --profile server config -q` — both valid,
  with or without `.env` (every interpolated variable has a default). A missing
  `manager.env` or `valheim.env` fails with an error naming the file.
- **Credential pipeline, end to end without a daemon:** hash written bare into
  `manager.env` → `docker compose config --format json` → unescape → `SessionAuth`. The
  hash arrives intact at 60 characters, validates as bcrypt, accepts the correct password
  and rejects a wrong one. (`docker compose config` renders `$` as `$$` because its output
  is itself a compose file; confirmed to be escaping, not data, by round-tripping the
  output through `config` three times — it stayed `$$` rather than doubling.)
  `tools/hash_password.py` was round-tripped through a subprocess: the line it prints is
  bare, 60 characters, and verifies against the password given.
- **After review (2026-09-12, post-patch):** `python -m pytest` — **98 passed** in a clean
  venv built from `requirements.txt`. Four fixes were mutation-tested by the reviewer
  independently of the implementer: re-capping the readiness scan, neutering `restart()`,
  disabling the WebSocket session re-check, and disabling the bcrypt byte limit each turned
  named tests red, and the suite returned to green after each restore. `docker compose config -q`
  and `--profile server config -q` are clean with the shipped example files in place, and fail
  by name when either env file is missing. The hash tool refuses an over-72-byte bcrypt
  password with exit 2 and no traceback, accepts exactly 72, counts bytes rather than
  characters (30 CJK characters = 90 bytes is refused), and succeeds for the same password
  under `--algo argon2`. Copying `manager.env.example` verbatim refuses to boot rather than
  running on a default credential, and `valheim.env.example` now ships an empty `SERVER_PASS`
  with `SERVER_PUBLIC=0`.
- `python -m pytest` against the manager package on the host (pre-review) — 41 passed, covering the
  whole I/O matrix (first-run create, pull failure, start-failure cleanup, readiness never
  arriving, readiness reset on restart, stop timeout → force stop, foreign origin, absent
  origin, unauthenticated page/API/WebSocket, session survival across restart, log
  backfill/dedupe, secret masking, SHA-hash rejection) plus the Compose-truncation
  regressions (truncated bcrypt, truncated and mangled argon2, quoted value, and intact
  hashes of both algorithms still verifying).
- The manager served locally (uvicorn) against a fake engine and driven in a real browser:
  login gate → sign in → Start → `pulling image` → `running (not yet ready)` → `ready` →
  Stop → `stopped`; killing and restarting the process showed the WebSocket reconnecting on
  its own with a backfill and the session still valid without re-login.

**Left for a host with a live Docker daemon:** `docker compose up -d --build`,
`docker compose exec manager python -m pytest`, a real first-run pull of the Valheim
image, confirmation of the readiness line against real server stdout, and a game-client
connection.
