# Valheim Manager

A self-hosted web dashboard for running a dedicated Valheim server in Docker: start and
stop it, watch the console, edit settings, and manage worlds, backups, mods and player
lists from a browser. It wraps the unmodified
[community-valheim-tools/valheim-server](https://github.com/community-valheim-tools/valheim-server-docker)
image, so the game server itself is the one most self-hosters already use.

It exists because the usual workflow (SSH in, edit an env file, `docker compose
restart`, tail the log, copy world files around with `scp`) is fine for one person and
tedious for a group where someone else wants to restart the server.

## Quick start

Requirements: Docker with Compose **v2.30+**, Linux containers on an **amd64** host
(the Valheim dedicated server is x86-64 only), and about 4 GB of disk for the game files.

```bash
git clone https://github.com/YakovShtef/valheim-manager.git
cd valheim-manager
docker compose up -d
docker compose logs manager     # prints a one-time setup URL
```

The log contains a block like this:

```
 FIRST-RUN SETUP REQUIRED -- no admin account exists yet.
 Open this one-time URL to create one:

   http://<this-host>:8080/setup?token=...
```

Open it and fill in the form. It asks for two different passwords:

| | Used for | Rules |
|---|---|---|
| Admin password | Signing in to the dashboard | At least 10 characters |
| Join password | What players type to enter the server | At least 5 characters, not part of the server name |

Then press **Start** in the dashboard. The first start downloads the game, which takes
a few minutes; progress shows in the console. When the status badge reads **ready**,
connect to `<host>:2456`.

No configuration file is required. The manager writes a default
`settings/valheim.env` on first boot and keeps its own credentials on a Docker volume.

Forward **UDP 2456-2457** (and 2458 for crossplay) for players outside your network.
Never forward the dashboard port (8080): the login has no rate limiting and no lockout.
On a VPS or any host with a public IP, put `MANAGER_BIND=127.0.0.1` in `.env` before
the first `docker compose up` and reach the dashboard over SSH or a VPN. See
[Security](#security).

## What it does

**Server control**
- Start, stop, restart and force-stop the game container. The manager creates the
  container itself, so it can report each phase: pulling, starting, loading the world,
  ready.
- Live console over WebSocket, with a filter, follow mode and copy.
- Uptime, CPU and memory for the running container.

**Settings**
- Server name, join password, port, public listing and crossplay.
- Valheim's world modifiers (preset, combat, death penalty, resources, raids, portals
  and the on/off toggles), composed into `SERVER_ARGS` without touching flags you added
  by hand.
- Editable only while the server is off; the next Start uses the new values. Comments
  and unknown keys in `valheim.env` are preserved.

**Worlds**
- List, load, create and delete worlds.
- Upload a world folder or a `.zip`, in the current format or the legacy `.db`/`.fwl`
  one. Loading a legacy world converts it to the current format permanently.
- Uploading is also the only way to use a specific seed: the dedicated server has no
  seed option.

**Backups**
- Back up any world on demand, or on a schedule (every N hours, or daily at a set time),
  keeping the last N automatic backups per world.
- Restore as a new world or over the original, download, or delete.
- The game image's own `worlds-*.zip` backups appear in the same list, with the worlds
  they contain.

**Mods**
- Per-world BepInEx mods: upload a `.zip` or `.dll`, enable, disable or remove. The
  loaded world's mods are synced into the plugins folder before the server starts.

**Players**
- A roster of everyone who has joined, built from the server log, and who is online now.
- Admin, ban and permitted (whitelist) lists, edited in the same files the game reads,
  with a raw-file view. The whitelist cannot be switched on while it is empty.
- No kick, broadcast or other in-game commands: the dedicated server has no RCON or
  command input. Those stay F5-console commands for an admin in the game.

## Configuration

Three optional files, each with a commented example in the repo.

| File | Purpose | Example |
|---|---|---|
| `.env` (repo root) | Compose settings: ports, bind address, image tag, limits | `manager/.env.example` |
| `manager.env` | Pin the admin credentials instead of using the setup wizard | `manager.env.example` |
| `settings/valheim.env` | Game settings, passed to the game container | `valheim.env.example` |

### `.env`

| Variable | Default | Notes |
|---|---|---|
| `MANAGER_PORT` | `8080` | Host port for the dashboard. |
| `MANAGER_BIND` | `0.0.0.0` | Use `127.0.0.1` on any host with a public IP. |
| `MANAGER_URL` | | Only used to print a clickable setup URL. |
| `COOKIE_SECURE` | `false` | Set `true` when the dashboard is served over HTTPS. |
| `ALLOWED_ORIGINS` | | Extra origins, comma separated. Needed behind a proxy that rewrites `Host`. |
| `SESSION_MAX_AGE_SECONDS` | `604800` | Session lifetime (7 days). |
| `MANAGER_UID` / `MANAGER_GID` | `10001` / `1000` | Keep `MANAGER_GID` equal to `PGID` in `valheim.env`. |
| `VALHEIM_IMAGE` | `ghcr.io/community-valheim-tools/valheim-server:latest` | Game server image. |
| `VALHEIM_CONTAINER_NAME` | `valheim-server` | |
| `VALHEIM_STOP_TIMEOUT` | `120` | Seconds the world gets to save on stop. Keep it above 60. |
| `VALHEIM_SETTINGS_DIR` | `./settings` | Host directory holding `valheim.env`. |
| `WORLD_UPLOAD_MAX_MB` / `WORLD_UPLOAD_MAX_FILES` | `1024` / `5000` | Upload a `.zip` for very large worlds. |
| `MOD_UPLOAD_MAX_MB` | `256` | Per mod file. |

### `manager.env` (credentials)

Set all three to skip the setup wizard, or none to use it. Compose loads this file with
`format: raw`, so **do not quote values**. Never put the hash in `.env`: Compose
interpolates `$` there and silently truncates bcrypt and argon2 hashes.

```ini
ADMIN_USER=admin
# docker compose run --rm --no-deps manager python tools/hash_password.py
ADMIN_PASSWORD_HASH=
# python -c "import secrets;print(secrets.token_urlsafe(48))"
SESSION_SECRET=
```

### Forgotten login

```bash
docker compose exec manager python tools/reset_admin.py && docker compose restart manager
```

It shows the current username (Enter keeps it) and asks for a new password twice.
Only the credential file is rewritten; settings, worlds, backups and the player roster
are untouched. Everyone signed in is signed out (`--keep-sessions` to avoid that). If
the manager is not running, use `docker compose run --rm --no-deps manager` in place
of `docker compose exec manager`. If the login comes from `manager.env`, change that
file instead.

### `settings/valheim.env`

The game image's own variables: `SERVER_NAME`, `WORLD_NAME`, `SERVER_PASS`,
`SERVER_PORT`, `SERVER_PUBLIC`, `CROSSPLAY`, `SERVER_ARGS`, `PUID`/`PGID`, `TZ` and so
on. The dashboard edits the common ones; anything else can be edited by hand and is
kept. The [image's README](https://github.com/community-valheim-tools/valheim-server-docker)
lists them all.

Leave `ADMINLIST_IDS`, `BANNEDLIST_IDS` and `PERMITTEDLIST_IDS` unset. The image
rewrites the list files from them on every start, which undoes changes made in the
Players tab.

### Volumes

| Volume | Mounted at | Contents |
|---|---|---|
| `valheim-config` | `/config` | Worlds, backups, mods, permission lists (shared with the game) |
| `valheim-data` | `/opt/valheim` | Game install and BepInEx |
| `valheim-manager-state` | `/srv/state` | Admin credentials (mode 0600), backup schedule, player roster |

## Security

- The login has **no rate limiting**. Keep the dashboard on your LAN, or bind it to
  `127.0.0.1` and reach it over SSH (`ssh -L 8080:127.0.0.1:8080 host`) or a VPN.
- The manager never touches the Docker socket directly. A
  [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy) on an internal
  network allows only container inspect, create, start, stop, remove and logs, plus image
  pulls. Exec, build, volumes, networks and the rest are revoked in `docker-compose.yml`.
- The manager runs as uid 10001 with `cap_drop: [ALL]` and `no-new-privileges`. A
  one-shot `permissions` container with no network fixes group ownership on the shared
  volumes at startup, so the manager never needs root.
- Sessions are signed `SameSite=Strict` cookies, and every state-changing request must
  carry a same-origin `Origin` header.

## Local development

The backend is FastAPI. The frontend is Jinja templates with vanilla JS and
hand-written CSS in `manager/app/templates` and `manager/app/static`; there is no
frontend build step.

```bash
python -m pip install -r manager/requirements-dev.txt
(cd manager/app/tests/js && npm install)    # jsdom, for the browser-side tests
cd manager && python -m pytest -q
```

The jsdom tests fail rather than skip when Node is missing.

To run the dashboard outside Docker, point its paths at a scratch directory. It runs
without a reachable Docker engine too; the status then reports that it cannot reach
Docker.

```bash
D=~/valheim-dev
mkdir -p $D/settings $D/state $D/config/{worlds_local,backups,mods,bepinex/plugins} $D/game-plugins
export VALHEIM_ENV_FILE=$D/settings/valheim.env \
       MANAGER_STATE_FILE=$D/state/manager-state.json \
       BACKUP_SCHEDULE_FILE=$D/state/backup-schedule.json \
       PLAYERS_FILE=$D/state/players.json \
       VALHEIM_CONFIG_DIR=$D/config \
       VALHEIM_WORLDS_DIR=$D/config/worlds_local \
       VALHEIM_BACKUPS_DIR=$D/config/backups \
       VALHEIM_MODS_DIR=$D/config/mods \
       VALHEIM_STAGING_PLUGINS_DIR=$D/config/bepinex/plugins \
       VALHEIM_GAME_PLUGINS_DIR=$D/game-plugins \
       DOCKER_HOST=unix:///var/run/docker.sock \
       MANAGER_URL=http://127.0.0.1:8080
cd manager && python -m uvicorn --factory app.main:create_app --reload --port 8080
```

The setup URL is printed to the terminal. Static files are cache-busted by a hash taken
at startup: `--reload` picks up Python and template changes, but restart after editing
anything in `static/`.

To build and run the image from your working tree:

```bash
docker compose up -d --build
```

### Layout

```
docker-compose.yml          game server, permissions fixer, socket proxy, manager
manager/
  Dockerfile                python:3.12-slim, runs as uid 10001
  app/
    main.py                 routes, WebSocket console, background tasks
    docker_control.py       container lifecycle through the socket proxy
    settings_store.py       comment-preserving writes to valheim.env
    worlds.py, backups.py   world files, uploads, backups and the schedule
    mods.py                 per-world mods
    players.py, player_log.py, permission_lists.py
    templates/, static/     server-rendered HTML, vanilla JS, CSS
    tests/                  pytest suite, plus jsdom tests in tests/js
  tools/hash_password.py    password hash for manager.env
  tools/reset_admin.py      new admin username/password for a forgotten login
```

## Contributing

Issues and pull requests are welcome. Run the test suite before opening a PR, and add a
test for any behaviour you change. Several tests pin user-facing wording on purpose; if
one fails after a copy change, update the test deliberately rather than loosening it.

## License

[MIT](LICENSE). The Valheim dedicated server and the game image it runs are separate
projects under their own terms.
