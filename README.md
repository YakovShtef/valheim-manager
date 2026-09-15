# Valheim Server Docker Stack with Live Log Console

Self-host a Valheim dedicated server in Docker and drive it from a browser: real
status, Start / Stop / Restart, and a live log console — no hand-typed `docker`
commands.

Three containers:

| Service | Image | Job |
|---|---|---|
| `valheim` | `ghcr.io/community-valheim-tools/valheim-server` (unmodified) | the game server |
| `manager` | built from `./manager` | login-gated FastAPI backend + WebUI |
| `docker-socket-proxy` | `tecnativa/docker-socket-proxy` | the only container that can see `/var/run/docker.sock` |

## Quick start

Needs Docker Compose **v2.30.0 or newer** (`docker compose version`) — see
[The `$` trap](#the--trap) for why.

```bash
docker compose up -d
docker compose logs manager      # copy the one-time setup URL it prints
```

There is nothing to copy and nothing to edit. The manager boots unconfigured and
prints a banner:

```
==========================================================================
 FIRST-RUN SETUP REQUIRED -- no admin account exists yet.
 Open this one-time URL to create one:

   http://<this-host>:8080/setup?token=Xk7q…

 Replace <this-host> with the address you reach the manager on (set
 MANAGER_URL in .env to have this printed ready to click).
 This URL works once, only until setup completes, and a manager restart
 replaces it. Nothing else is reachable until then.
==========================================================================
```

Open it. The wizard creates the single admin account, generates the manager's own
session secret, and writes your initial game settings to
`./settings/valheim.env`. You land signed in, and `/setup` closes permanently —
the token is void from then on, even in your own browser.

It asks for two different passwords, and they are not interchangeable: the **admin
password** signs you into this manager, and the **join password** is what players
type to enter the world. The join password is required and must be at least 5
characters — `valheim_server.x86_64` refuses to start without one, whatever
`SERVER_PUBLIC` says, and there is no passwordless option. If you later empty it by
hand, **Start** refuses and says so rather than handing you a container that exits a
second later.

Then press **Start**. The first start pulls the server image and downloads the game
files through SteamCMD, so it takes a while — the console shows exactly how far
along it is.

Two things the wizard deliberately does *not* do: it does not start the server, and
it does not create a world. **The world is created by Valheim itself on your first
Start, and its seed is chosen randomly at that moment and fixed from then on** —
the dedicated server takes no seed argument, so there is nothing to pick. Until you
press Start there is no game container and no world at all.

`docker compose up -d` deliberately starts only `manager` and
`docker-socket-proxy`. The game container is created by the manager on your first
**Start**, which is what lets the UI report *pulling → creating → starting →
running → ready* as distinct phases.

> **Native Linux hosts.** Docker creates a missing bind-mount source as `root`, so
> on a native Linux host the manager (uid 10001) may not be able to write
> `./settings`. If the wizard reports that it cannot write there, the simplest fix
> keeps the default uid:
>
> ```bash
> sudo chown -R 10001:10001 settings
> ```
>
> Setup stays open until it succeeds, so the same URL still works afterwards.
>
> The alternative is to run the manager as *your* uid, but the credential volume was
> initialised for 10001 and would then be unwritable — so it has to go too, which
> means setup runs again from scratch:
>
> ```bash
> docker compose down
> docker volume rm valheim-manager-state          # discards the admin account
> printf 'MANAGER_UID=%s\nMANAGER_GID=%s\n' "$(id -u)" "$(id -g)" >> .env
> docker compose up -d
> ```
>
> Docker Desktop (including WSL2) is unaffected by any of this.

## Where everything lives

Nothing below is required — the stack runs with none of these files present.

| Location | Holds | Written by |
|---|---|---|
| `valheim-manager-state` volume | admin user, password hash, session secret, mode `0600` | the setup wizard |
| `valheim-config` volume | the worlds (`/config/worlds_local`), the image's hourly backups | the game server; the manager adds uploaded worlds |
| `./settings/valheim.env` | game settings: `SERVER_NAME`, `SERVER_PASS`, `WORLD_NAME`, … | the manager on first boot, then you |
| `.env` *(optional)* | Compose knobs: ports, image, volume names, log tuning | you |
| `manager.env` *(optional)* | `ADMIN_USER`, `ADMIN_PASSWORD_HASH`, `SESSION_SECRET` — the advanced path below | you |

`.env`, `manager.env` and `settings/` are gitignored.

The manager owns `settings/valheim.env` only in the weakest sense: it creates the
file with documented defaults when none exists, and after that it *only ever
replaces the values of the keys it was asked to change*. Comments, blank lines, key
order, `export ` prefixes, inline comments and any extra keys you add all survive,
and every write goes through a temp file and `os.replace` so the `valheim` service
never reads a half-written file. Put your own file at `settings/valheim.env` before
the first `docker compose up -d` and it is left byte-identical —
`valheim.env.example` is a ready-made copy.

Credentials are not in a file you edit at all, which retires a whole class of
install failure — see the trap below.

### The `$` trap

Compose interpolates `$` in `.env` *and* in a default-format `env_file:`. Every
bcrypt hash is `$2b$12$…` and every argon2 hash is `$argon2id$v=19$…`, so `$12`,
`$argon2id` and the digest tail are read as variable references and replaced with
blank strings. `$2b$12$abc…` silently becomes `$2b$12`; an argon2 hash loses its
prefix entirely. Nothing errors — you just get "Invalid username or password"
forever.

The setup wizard sidesteps this completely: the hash it generates goes straight to a
volume that Compose never reads. The trap only applies to the advanced path below,
where `manager.env` is loaded with `format: raw` (Compose **v2.30.0+**), the only
loader that passes a value through untouched. Raw format also takes quotes
*literally*, so values there must be bare — the opposite of the rule in `.env`.

The manager still recognises a wrecked hash at startup — truncated bcrypt, mangled
argon2, or a quoted value — and fails with an error naming this exact cause, instead
of failing at the login screen with no explanation. That check has not been relaxed:
a manager with credentials present refuses to boot on a bad hash or a short secret,
and never falls back to reopening the wizard.

## Advanced: configuring credentials yourself

Setting all three of `ADMIN_USER`, `ADMIN_PASSWORD_HASH` and `SESSION_SECRET` in
`manager.env` skips first-run setup entirely: no token is logged, `/setup` is never
available, and login behaves exactly as it did before the wizard existed. Useful for
scripted deployments, a secrets manager, or one hash shared across hosts.

It is all three or none — a partial set refuses to boot rather than silently handing
you an admin-creation page.

```bash
cp manager.env.example manager.env
docker compose run --rm --no-deps manager python tools/hash_password.py            # bcrypt
docker compose run --rm --no-deps manager python tools/hash_password.py --algo argon2
python -c "import secrets;print(secrets.token_urlsafe(48))"                        # SESSION_SECRET
```

The password is read without echoing and nothing is written to disk. Copy the
printed line into **`manager.env`**, exactly as printed — no quotes, and not into
`.env`. `SESSION_SECRET` must be at least 16 characters and kept stable: it signs
the session cookie, so a stable secret is what keeps you signed in across
`docker compose restart manager`. Change it and every existing session is
invalidated.

Do not move these three into the manager's `environment:` block in
`docker-compose.yml`: `environment:` overrides `env_file:` for the same key, so that
would reintroduce the interpolation bug.

To go back to the wizard, remove the three values **and** the state file:
`docker volume rm valheim-manager-state` (this discards the admin account and signs
out every session).

## Resetting the admin account

There is no password-change UI in this build. Delete the credential volume and run
setup again:

```bash
docker compose down
docker volume rm valheim-manager-state
docker compose up -d
docker compose logs manager      # a fresh setup URL
```

Worlds, backups and settings are untouched by this — they live on `valheim-config`
and in `./settings`. Every existing session is invalidated, because the new admin
account comes with a newly generated session secret.

## What the UI shows

The dashboard is three tabs, and the server's phase badge sits in the tab strip so it
is on screen whichever one you are on:

- **Console** — the landing tab: the status card, **Start** / **Stop** / **Restart**,
  and the live log. It keeps streaming while another tab is showing; panels are
  hidden, never rebuilt, so coming back finds the scrollback and the *follow*
  checkbox as you left them. With *follow* on, returning to the Console jumps to the
  newest line — a hidden panel cannot scroll, so following is re-established on the
  way in rather than leaving you parked above the tail. With *follow* off, the
  position you were reading at is left alone.
- **Server settings** — the values in `settings/valheim.env`, and the editor.
- **Worlds** — the worlds on the volume, **Load**, and the upload drop zone.

Settings and Worlds stay open while the server runs so their current values can be
read; their editing controls are disabled with the reason spelled out rather than
hidden, and the tab is marked *locked* in the strip. A world upload in flight marks
its tab *uploading*, and a worlds error marks it *error*, so neither is invisible
from another tab. The selected tab is remembered per browser and comes back on
reload — anything unrecognised lands on Console. Left and Right (and Home / End)
move along the strip, Enter or Space activates, and each panel can take focus itself
so the keyboard reaches a panel whose controls are all locked.

*Loading world* and *ready* are different facts and the UI never conflates them:

- **loading world** — the server is up, but the world is still loading. Nobody can
  join yet.
- **ready** — the server printed its own `Game server connected` line. Only then is
  it accepting players.

If that line never arrives, the badge stays on *loading world* instead of claiming
readiness. If a game update changes the wording, set `READY_LOG_PATTERN` in `.env`
to a regex that matches the new line.

The console backfills the last 200 lines and appends new ones within about a
second. If the WebSocket drops, it reconnects on its own (exponential backoff up to
15 s) and backfills again — no page reload.

`SERVER_PASS` and anything else whose name looks like a secret are masked in the
settings panel — including while you edit it, where leaving the mask in place keeps
the stored value. The real join password is never rendered into the page and never
logged. Editing the panel is covered under "Changing settings after setup" below.

## Security notes

**Scoped Docker access.** The manager never sees the Docker socket and never shells
out to the `docker` CLI. It speaks the Engine API over
`DOCKER_HOST=tcp://docker-socket-proxy:2375`, and the proxy's allowlist in
`docker-compose.yml` grants only what the manager calls:

```
CONTAINERS  inspect / create / remove / logs
IMAGES      inspect + pull on first run
POST        write methods at all
ALLOW_START / ALLOW_STOP / ALLOW_RESTARTS
```

Everything else — `EXEC`, `BUILD`, `SWARM`, `SECRETS`, `VOLUMES`, `NETWORKS`,
`SYSTEM`, `INFO`, `EVENTS` — is explicitly revoked. The proxy sits on an
`internal: true` network, so nothing on the host or on the game network can reach
it. Access is still powerful enough to create containers, so treat the manager
login as root-equivalent on this host.

**Exposure depends on your host — check before you trust it.** `MANAGER_BIND`
defaults to `0.0.0.0`, which publishes the WebUI on *every* interface the host has.

- On a home machine behind NAT, that means your LAN only, and it stays that way
  unless you forward the port on your router — don't.
- **On a VPS, cloud instance, or any host with a routable public address, `0.0.0.0`
  is immediately reachable from the internet.** There is no rate limiting or lockout
  on this login (see below), so do not leave the default there. Set
  `MANAGER_BIND=127.0.0.1` and reach it over an SSH tunnel or VPN, or restrict the
  port with a host firewall / cloud security group.

For deliberate remote access, put it behind a VPN, or behind a reverse proxy with TLS
and set `COOKIE_SECURE=true` plus `ALLOWED_ORIGINS=https://your.host`.

**Login gate.** Nothing behind the gate leaks: `/` redirects to `/login`, the API
returns `401`, and `/ws/logs` is refused before the WebSocket handshake completes,
so an unauthenticated client never receives a log line. Wrong credentials give one
generic error with no user enumeration.

**First-run setup is token-gated, and that is the point.** Because the manager's
login is root-equivalent on this host, an *open* admin-creation page would hand the
service to whoever loaded it first — a race anyone on the LAN could win. So the
wizard needs the one-time token printed in the manager's log, which is why install is
`docker compose up -d` plus one URL rather than `docker compose up -d` alone (the
Jupyter/Portainer precedent). The specifics:

- The token exists only in the manager's memory. It is never written to the state
  file, so restarting the manager mints a new one and the old URL stops working.
- It is void the moment setup completes. Reopening `/setup` afterwards — even with
  the original token, even in the browser that just used it — redirects to `/login`
  and overwrites nothing.
- A wrong, missing or reused token gets one generic refusal that says nothing about
  whether a token is outstanding, and does not burn the real one.
- While unconfigured, *every* other route redirects to `/setup`, and the log
  WebSocket refuses the handshake. `/healthz` and `/static` are the exceptions: a
  manager awaiting setup is running correctly, so reporting it unhealthy would be a
  lie — it would show as `(unhealthy)` in `docker ps`, hold up anything gated on
  `depends_on: condition: service_healthy`, and send you hunting a fault instead of
  reading the setup URL. (Nothing is restarted over it either way: Docker Engine and
  Compose act on process exit, never on health status — only Swarm or an autoheal
  sidecar does that.)
- Anyone who can read `docker compose logs` on the host can complete setup. That is
  already true of anyone who can reach the Docker socket, so it is not a new
  boundary — but treat the log as sensitive until setup is done.

**Origin check.** `POST /api/start|stop|restart|settings|worlds/switch|worlds/upload`
(and `/login`, `/logout`) require an `Origin` header matching the server's own host,
or one listed in `ALLOWED_ORIGINS`. A request with a valid session cookie but a
foreign or absent `Origin` is rejected before any Docker call is made — and, for an
upload, before its body is read.

**The game volume.** The manager mounts `valheim-config` read-write so the Worlds
panel can see `/config/worlds_local`. It only ever *adds* worlds there: there is no
delete path in this build, uploads are refused rather than allowed to overwrite an
existing world, and every name — typed, dropped, or read out of an archive — is
reduced to a single path segment and re-resolved against the destination before
anything is written. An archive entry that would land outside the world's own
directory is refused and nothing is written anywhere.

**Signing out is not revocation.** `POST /logout` clears the cookie in *that* browser,
but the token it held stays cryptographically valid until
`SESSION_MAX_AGE_SECONDS` elapses (7 days by default). A cookie copied off the machine
beforehand therefore still works after you sign out. Real revocation needs a token
epoch persisted across manager restarts, which this build deliberately does not take
on; the available mitigation is to shorten `SESSION_MAX_AGE_SECONDS` (say to a few
hours) so a leaked cookie ages out quickly. Rotating `SESSION_SECRET` invalidates every
token immediately if you need that now.

An open log WebSocket is re-checked against the session on every status push, so an
expiring session drops the stream rather than streaming on until the socket happens to
close. That check sees expiry, not a sign-out elsewhere — for the reason just given.

There is deliberately **no login rate limiting or lockout** — accepted trade-off for
a single-operator LAN tool. Use a long password; the wizard requires at least 10
characters for exactly this reason. With bcrypt only the first 72 bytes count, so
both the wizard and the hash tool refuse anything longer rather than hashing a
silently truncated password, and both offer argon2, which has no such limit.

**Credentials at rest.** The state file is written at mode `0600` on a volume no
other service mounts, through a temp file whose mode is set *before* the rename, so
the hash and session secret are never briefly world-readable. The password, the hash
and the secret are never logged. A state file that exists but cannot be read as a
complete document refuses the boot and names itself — it never degrades into
"unconfigured", which would reopen admin creation on a manager that already has an
admin.

## Deploying on a Linux server (CasaOS, Debian, a NAS)

This stack **builds the manager image from source**, so the server needs the repo —
not just a compose file. That rules out pasting a compose into CasaOS's "Custom
Install" box, which expects prebuilt images. Clone and bring it up over SSH instead;
CasaOS lists the running containers afterwards either way.

```bash
sudo apt install -y git
git clone <your-repo-url> valheim-manager
cd valheim-manager
```

**Check Compose first.** The optional `manager.env` path uses `env_file: format: raw`,
which needs Compose **v2.30.0+**:

```bash
docker compose version
```

If yours is older, either upgrade Docker or delete the four `env_file:` lines under
the `manager` service — the setup wizard writes credentials to a volume, so that block
exists only for the advanced path below.

**Then the one Linux-only step.** Docker creates a missing bind-mount source as
`root`, and the manager runs as uid 10001, so it cannot write its settings file unless
the directory exists and belongs to it:

```bash
mkdir -p settings && sudo chown -R 10001:10001 settings
docker compose up -d
docker compose logs manager      # copy the one-time setup URL
```

Docker Desktop on Windows and macOS does not need this; native Linux does.

**Reaching it.** The UI listens on port 8080 and binds every interface. CasaOS itself
uses port 80, so there is no clash, but if 8080 is taken put `MANAGER_PORT=8099` (or
anything free) in a `.env` beside the compose file. Players need UDP **2456-2458**
forwarded to this machine; the web UI port should *not* be forwarded — reach it over
your LAN, a VPN, or Tailscale.

**If the socket proxy exits immediately**, see [If the socket proxy will not
start](#if-the-socket-proxy-will-not-start) — one capability line is the likely cause.

## Two ways to run the game container

The manager owns the game container. `docker-compose.yml` also defines the
`valheim` service, behind the `server` profile, as the canonical shape of what the
manager creates and as an escape hatch:

```bash
docker compose --profile server up -d valheim   # bypass the manager
```

The two paths share the container name and volumes, so use one or the other, not
both at once. The manager creates the `valheim-config` / `valheim-data` volumes on
its first Start; if you take the profile path on a fresh host first, Compose creates
them instead. Changing published ports is the one place the two can drift: the
manager derives them from `SERVER_PORT` in `settings/valheim.env`, while the profile
path uses `VALHEIM_PORTS` in `.env`.

## Changing settings after setup

The **Current settings** panel on the dashboard is editable whenever the server is
off — meaning no container at all, or one that is not running, restarting or paused.
Press **Edit**, change what you need, press **Save**. That does three things:

1. **Validates**, exactly as the setup wizard does — same rules, same messages,
   including the join password's 5-character minimum and the world name's no-slash
   rule. A bad value is refused with the offending field named, and nothing is
   written.
2. **Rewrites `settings/valheim.env` in place**, atomically, replacing only the values
   of the keys the panel owns. Your comments, blank lines, key order, `export`
   prefixes, inline comments and any other keys (`TZ`, `BACKUPS`, whatever you added by
   hand) survive byte for byte.
3. **Removes the stopped container**, so the next **Start** creates a new one from the
   new file. This step is the whole feature: a container keeps the environment it was
   created with, so Start or Restart on their own would quietly run the old settings.

Your world, your backups and the server install live on the `valheim-config` and
`valheim-data` volumes. The container removal does not touch them.

A save that changes nothing writes nothing and removes nothing. The join password is
shown as `********`: leave the mask exactly as it is to keep the stored password, or
replace it to change it — the mask itself is never written as a value.

While the server is running, restarting or paused the panel stays read-only and says
why. There is no apply-and-restart: stop the server first. The server-off condition is
re-checked by the manager when the save arrives, not just in the browser, so a server
started from another tab or straight from the host in the meantime gets the save
refused rather than a rewrite behind its back.

`WORLD_NAME` is the one field to think twice about: it selects which save to load, so
pointing it at a name that does not exist yet makes the next Start generate a *new*
world with a new random seed rather than changing the old one's. The old world stays
on the volume. To move between worlds that already exist, use the **Worlds** panel
below instead — it only offers names that are really there.

### World modifiers

The same panel carries Valheim's world modifiers — difficulty and the rules that go
with it. They are **launch arguments**, not world data: they change how the server
plays, and never alter or migrate the save you already have. Like every other value
here they reach the game when a new container is created, which is what Save (removes
the stopped container) and then **Start** does.

| Control | Written as | Values |
|---|---|---|
| Preset | `-preset <name>` | `casual`, `easy`, `hard`, `hardcore`, `immersive`, `hammer` |
| Combat difficulty | `-modifier combat <value>` | `veryeasy`, `easy`, `hard`, `veryhard` |
| Death penalty | `-modifier deathpenalty <value>` | `casual`, `veryeasy`, `easy`, `hard`, `hardcore` |
| Resource rate | `-modifier resources <value>` | `muchless`, `less`, `more`, `muchmore`, `most` |
| Raid frequency | `-modifier raids <value>` | `none`, `muchless`, `less`, `more`, `muchmore` |
| Portal rules | `-modifier portals <value>` | `casual`, `hard`, `veryhard` |
| Building costs nothing | `-setkey nobuildcost` | on/off |
| Raids follow players | `-setkey playerevents` | on/off |
| Creatures never attack | `-setkey passivemobs` | on/off |
| No map and no minimap | `-setkey nomap` | on/off |

They all land in one key, `SERVER_ARGS`, and the panel shows the exact string it is
about to write before you save it. Three rules shape that string:

- **Order.** `-preset` first, then every `-modifier`, then every `-setkey`. Valheim
  applies these in sequence, so a preset written *after* a category silently flattens
  it back — the manager composes the string itself for exactly this reason.
- **Defaults are omissions.** Every category also has a `normal` setting, which is what
  you get when the argument is absent, and which Valheim does not document as an
  argument value. So a row left on its default writes nothing rather than
  `-modifier combat normal`, and setting a row back to its default removes just that
  argument.
- **Your own arguments survive.** Anything already in `SERVER_ARGS` that is not one of
  the modifiers above is kept exactly as you wrote it and moved after them. Changing a
  dropdown never costs you a flag you added by hand.

A value outside the list is refused with the field named, and nothing is written —
the same refusal the rest of the panel gives.

### Advanced: editing the file by hand

The file is yours and the manager never overwrites it, so editing
`settings/valheim.env` on the host stays supported — including for keys the panel does
not offer. The change still has to reach a new container, which is the same three steps
the panel automates:

1. Edit `settings/valheim.env`.
2. Press **Stop** in the WebUI and wait for the badge to read *stopped*. (Equivalently
   on the host: `docker stop valheim-server`. The container is not part of the default
   Compose profile, so `docker compose stop valheim` needs `--profile server` to
   resolve it at all.)
3. Remove the stopped container, then press **Start**:

```bash
docker rm valheim-server          # or "$VALHEIM_CONTAINER_NAME" if you changed it
```

Start then creates a new container from the edited file. `docker rm` refuses a running
container, which is why step 2 has to finish first.

(Not built yet: a "Backup now" button, mod toggles, admin/ban-list editing, backup
browse and restore, and deleting a world from the UI.)

The image keeps taking its own hourly world backups into `/config/backups` on the
`valheim-config` volume regardless.

## Worlds: list, switch, and upload

The **Worlds** panel lists what is actually on the `valheim-config` volume under
`/config/worlds_local` — each world with its layout, its size, and the active one
marked — lets you switch which one the server loads, and accepts a world dragged in
from your own PC. Like the settings panel it works **only while the server is off**,
and the manager re-checks that for itself when the request arrives.

**Switching.** Press **Load** on a world. That writes `WORLD_NAME` through the same
validated path the settings panel uses and removes the stopped container, so the next
**Start** loads that world. Only worlds the manager can actually see are offered:
picking one can never leave you with a brand-new empty world the way a typo in
`WORLD_NAME` can.

**Uploading.** Drop a world onto the panel (or use *Choose a folder…* / *Choose
files…*). This is the only way to choose a world **seed** — `valheim_server.x86_64`
takes no `-seed` argument, so a world with the seed you want has to be generated in
the game and imported here. On Windows, a local world lives in
`%USERPROFILE%\AppData\LocalLow\IronGate\Valheim\worlds_local`.

| What to drop | Recognised as | Notes |
|---|---|---|
| The world's folder | 1.0 world | Must hold `_main.N.db2` and `_main.N.fwl2`. Named after the folder. |
| A `.zip` of that folder | 1.0 world | One archive at a time. Best for a big world — a 1.0 world is one file per visited map chunk, and that can be thousands of them. |
| A matching `.db` + `.fwl` pair | pre-1.0 world | Both files, together. |

Two limits apply per upload, both stated in the panel and both refused with the limit
named: `WORLD_UPLOAD_MAX_MB` (1024 MB by default) and `WORLD_UPLOAD_MAX_FILES` (5000).
The file count is the one a *folder* drop runs into, since every visited map chunk is
its own file — a `.zip` is a single file whatever the world's size, which is why it is
the better route for a well-explored world.

Three things the upload will not do:

- **Overwrite.** A name already in use on the volume is refused, and what is standing
  there is named. That check reads the directory itself rather than the world list, so
  a *half* world — a lone `.db`, or a pair whose halves are spelled differently — is in
  the way too, even though the list does not show it. Give the upload a different name
  in *Name on the server* and try again.
- **Half-write.** The upload is validated in full, written to a hidden staging
  directory, and only then moved into place under its real name. Until that last
  rename nothing on the volume carries the world's name, so there is no moment at
  which the server could load a partly-written world.
- **Trust the archive.** Entry names are re-rooted and resolved first: an entry
  named `../escape`, an absolute path, or a symbolic link is refused before
  extraction starts and nothing is written anywhere. A body whose declared size is
  past `WORLD_UPLOAD_MAX_MB` is refused before the volume is touched at all — before
  it is read, in fact — which is also why an upload that does not declare its size is
  refused rather than being let through the gate. Every refusal is logged on the host.

**Pre-1.0 worlds convert, permanently.** A `.db` / `.fwl` pair still loads, but the
first time the server opens it Valheim rewrites it into the 1.0 folder layout and
there is no way back. The panel says so before and after the upload; keep your own
copy of the pair if you may want the old format again.

### The shared group (`MANAGER_GID`)

The game server runs as `PUID:PGID` from `settings/valheim.env` (1000 by default) and
saves to its world continuously. The manager runs as its own uid with `cap_drop:
[ALL]`, so it **cannot** `chown` what it writes — there is no CAP_CHOWN and it is not
root. A world uploaded under the wrong group would be readable but not writable, and
that surfaces much later as the server silently failing to save.

So the manager runs with the *game's group* and writes uploaded worlds
group-writable: `user: "${MANAGER_UID:-10001}:${MANAGER_GID:-1000}"` in
`docker-compose.yml`. **Keep `MANAGER_GID` equal to `PGID`.** If you change `PGID` in
`settings/valheim.env`, set `MANAGER_GID` to match in `.env`.

The worlds directory itself belongs to the game server, which normally creates it on
its first **Start**. On a brand-new volume the manager will create it if `/config`
lets it, and say so plainly if it cannot — in which case press Start once and upload
afterwards. If the manager reports that it cannot write into an existing worlds
directory, make it group-writable once, from the host:

```bash
docker run --rm -v valheim-config:/config alpine \
  sh -c 'chmod 2775 /config/worlds_local'
```

Nothing here deletes a world. Removing one is still a host operation, on purpose.

## Verify

No configuration files are needed for any of these.

```bash
docker compose config -q                 # valid with no env files present
docker compose --profile server config   # ... including the game service
docker compose up -d --build             # manager + proxy up, WebUI on :8080
docker compose logs manager              # the first-run setup URL

# The edge-case suite, from a checkout. It reads repo-root files
# (valheim.env.example, docker-compose.yml) that are outside the ./manager build
# context, so it does not run inside the manager container -- and pytest is
# deliberately not installed in the runtime image.
python -m pip install -r manager/requirements-dev.txt

# Part of the suite runs the dashboard's app.js in jsdom rather than reading it as
# text -- which panel is showing, whether the console kept its buffer, whether a
# lock reached the tab strip are all runtime facts a source-text check cannot see.
# Once, and node has to be on PATH. These tests FAIL rather than skip without it:
# they are the only checks behind the tab strip's behaviour.
cd manager/app/tests/js && npm install && cd ../../../..

cd manager && python -m pytest
```

Manual pass, from a tree with no `.env`, no `manager.env` and no `settings/`:

1. `docker compose up -d`, then complete the wizard at the logged URL. Confirm you
   land signed in without a second login.
2. Confirm the credential file is `0600` and holds no plaintext password:
   `docker compose exec manager ls -l /srv/state/manager-state.json`
3. Reload `/setup` with the same token — it must redirect to `/login`, and
   `settings/valheim.env` and the state file must be unchanged.
4. `docker compose restart manager` — the session survives and setup stays closed.
5. Before pressing Start: `docker ps -a` shows no game container, and
   `docker run --rm -v valheim-config:/c busybox ls /c/worlds_local` is empty. No
   world exists yet.
6. First run: **Start** → watch *pulling → creating → starting → running → ready*,
   then **Stop**, then **Start** again; each badge should match
   `docker inspect -f '{{.State.Status}}' valheim-server`.
7. Compare the console against `docker compose logs -f valheim`.
8. Connect a Valheim client to `<host>:2456` once the badge says **ready**.
9. With the server stopped, add a flag of your own to `SERVER_ARGS` in
   `settings/valheim.env` by hand, then press **Edit**, pick a preset and a couple of
   modifiers, and confirm the previewed string is what lands in the file — with your
   flag still there, after the modifiers. With the server running, the panel must be
   read-only and say why, modifiers included.

### If the socket proxy will not start

Both services run with `cap_drop: [ALL]`. That is correct as far as we can tell —
haproxy binds 2375, an unprivileged port — but it was validated only with
`docker compose config`, never against a running engine, because no Docker daemon was
available while this was built. Some haproxy images drop privileges internally at
startup and need `CAP_SETUID`/`CAP_SETGID` to do it.

So if `valheim-docker-socket-proxy` exits immediately and its logs mention
permissions, capabilities or setuid, relax that one line in `docker-compose.yml`:

```yaml
    # cap_drop: [ALL]
    cap_add: [SETUID, SETGID]
```

Keep `security_opt: ["no-new-privileges:true"]` either way — it cannot cause this and
is the more valuable of the two. The endpoint allowlist above it, not the capability
set, is what actually confines this container.

## Layout

```
docker-compose.yml            three services, scoped proxy allowlist
manager.env.example           OPTIONAL credential override (copy to manager.env)
valheim.env.example           reference copy of the game settings the manager writes
settings/valheim.env          created on first boot; yours to edit afterwards
manager/
  Dockerfile                  python:3.12-slim, runs as uid 10001
  .env.example                optional Compose-level knobs (copy to ./.env)
  requirements.txt            runtime dependencies, the only ones in the image
  requirements-dev.txt        those plus pytest, for running the suite from a checkout
  tools/hash_password.py      ADMIN_PASSWORD_HASH generator (advanced path only)
  app/
    main.py                   FastAPI routes, credential resolution, /ws/logs
    setup.py                  first-run token, the shared field validation, the write
    state_store.py            the 0600 credential state file
    auth.py                   bcrypt/argon2 + signed SameSite=Strict cookie
    docker_control.py         Engine API via the proxy; readiness detection
    settings_store.py         read + atomic comment-preserving write of valheim.env
    modifiers.py              the world-modifier vocabulary, parsed to and from
                              SERVER_ARGS
    worlds.py                 the worlds on the game volume: listing, upload
                              validation, staged group-writable placement
    templates/ static/        server-rendered HTML + vanilla JS, no build step
    tests/test_edge_cases.py  I/O-matrix edge cases against a fake engine
```

Requires Linux containers (native Linux host, or Docker Desktop on WSL2).
Windows-container / Hyper-V mode is not supported.
