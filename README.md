# 🛡️ Valheim Server + Web Dashboard

Host a Valheim server for you and your friends, and run the whole thing from a web
page instead of typing commands.

Press **Start**, watch the log scroll past, press **Stop** when everyone logs off.
Change the server name, swap which world is loaded, drag a world across from your
own PC — all from a browser tab, on your phone if you like.

You don't need to know Docker. You need to be able to copy and paste three
commands, once.

---

## What you get

- ▶️ **Start, Stop and Restart buttons** — no terminal once you're set up
- 📜 **A live log**, exactly what the server is printing, as it prints it
- ⚙️ **A settings page** for the server name, password, port and difficulty
- 🌍 **World management** — switch worlds, or upload one from your own game
- 🔒 **A login**, so nobody else on your network can press Stop mid-raid

The game server itself is the well-known community Valheim server image,
completely unmodified. This project adds the dashboard around it.

---

## 🚀 Get playing in three steps

### What you need first

- A computer to host it on that stays awake — a spare PC, a home server, a NAS
- **Docker** installed, with Compose **v2.30 or newer**. Check with
  `docker compose version`. (Docker Desktop on Windows or macOS already includes it.)
- About 4 GB of free disk space for the game files

That's it. There's nothing to configure before you start, and no file to edit.

### Step 1 — Download it and bring it up

```bash
git clone <your-repo-url> valheim-manager
cd valheim-manager
docker compose up -d
```

### Step 2 — Open the setup link

The dashboard prints a one-time link to its log. Fetch it with:

```bash
docker compose logs manager
```

Look for this:

```
==========================================================================
 FIRST-RUN SETUP REQUIRED -- no admin account exists yet.
 Open this one-time URL to create one:

   http://<this-host>:8080/setup?token=Xk7q…
==========================================================================
```

Copy the whole URL, swap `<this-host>` for the machine's address (`localhost` if
you're sitting at it), and open it in a browser.

The page asks for **two different passwords**, and mixing them up is the single most
common trip-up:

| | What it's for | Rules |
|---|---|---|
| **Admin password** | Signing in to *this dashboard* | At least 10 characters |
| **Join password** | What your friends type to enter the world | At least 5 characters, and it can't be part of the server name |

Valheim flatly refuses to start without a join password, so there's no way to skip
it. Fill both in, pick a server name and a world name, and finish.

You land signed in, and that setup link stops working forever. 🎉

### Step 3 — Press **Start**

The first start downloads the actual game files, which takes a few minutes. The
console shows you how far along it is, so you're never staring at a blank screen.

Watch the badge at the top. When it says **ready**, your server is accepting
players.

---

## 🎮 Getting everyone in

Once the badge says **ready**:

- **You, on the same network:** open Valheim → Join Game → Join IP, and enter the
  host machine's local address and port, like `192.168.1.50:2456`.
- **Friends over the internet:** forward **UDP ports 2456, 2457 and 2458** on your
  router to the host machine. That's the usual "port forwarding" page in your router
  settings.

Everyone types the **join password** when they connect.

> ⚠️ Forward the *game* ports only. Don't forward port 8080 — that's the dashboard,
> and it should stay on your own network. More on this in
> [Keeping it safe](#-keeping-it-safe).

---

## 🖥️ Using the dashboard

Three tabs across the top. The server's status badge sits in that strip, so you can
always see what the server is doing no matter which tab you're on.

### Console

The page you land on: the status card, **Start** / **Stop** / **Restart**, and the
live log.

The log keeps running while you're on another tab — switch to Worlds, come back, and
nothing's missing. Leave **follow** ticked and it stays pinned to the newest line;
untick it to scroll back and read something without being yanked to the bottom.

The badge tells you where the server is up to:

| Badge | What's happening |
|---|---|
| **off** | Nothing running |
| **downloading** | Fetching the server image, first time only |
| **setting up** | Preparing to launch |
| **starting** | Booting |
| **loading world** | Up, but still loading the map — nobody can join yet |
| **ready** | ✅ Accepting players |
| **problem** | Something went wrong — the console will say what |

**loading world** and **ready** are genuinely different things, and the dashboard
never pretends otherwise. It only says *ready* once the server itself reports it's
connected. If you join too early you'll just bounce off, so wait for the green one.

### Server settings

Your server's name, password, port and Valheim's difficulty options.

You can look at these any time, but you can only **change** them while the server is
off — the tab says so, and shows a **LOCKED** tag when that's the case. Stop the
server, press **Edit**, change what you want, press **Save**, then press **Start**.

The join password shows as `********`. Leave those stars alone to keep your current
password; type over them to change it.

### Worlds

Every world saved on your server, which one is loaded, and a box you can drag a
world into. Also only available while the server is off. See
[Worlds](#-worlds-switching-and-adding-new-ones) below.

---

## 🌍 Worlds: switching and adding new ones

Stop the server first — both of these need it off.

### Switching to a different world

Press **Load** next to any world in the list. Then press **Start**, and that's the
world your server opens.

The list only offers worlds that genuinely exist, so you can't accidentally end up
in a brand-new empty world this way.

### Starting a fresh world

Type a name into **Make a new world** and press **Create**, then press **Start**.
Valheim builds the world at that moment, with a random seed it picks itself.

Your current world isn't touched — it stays in the list and you can switch back to it
whenever you like. If the name is already taken, the dashboard says so and points you
at **Load** instead of quietly opening the world that's already there.

### Backing up a world

Press **Backup** on any world. It writes a `.zip` into the same folder the game keeps
its own hourly backups in (`/config/backups`), named `MANUAL-<world>-<date>.zip`.

Two things make it different from the automatic ones:

- **It works while people are playing.** Every other control here needs the server
  off; this one doesn't, because it only reads the world and writes somewhere else.
  What you get is a copy of whatever the server last saved to disk — same as the
  game's own hourly backup.
- **It's never deleted on a timer.** The game prunes its own backups after
  `BACKUPS_MAX_AGE` days (3 by default), but only files matching its own naming. A
  `MANUAL-` one falls outside that, so it stays until you remove it yourself.

If the zip can't be finished for any reason, nothing is left behind that looks like a
backup — the archive is built under a temporary name and only renamed once complete.

### Deleting a world

Press **Delete** on any world and you'll get a confirmation naming the world and its
size. It removes the save from the server for good, and the dashboard can't bring it
back — though if the game's hourly backups are switched on, a copy may still be in the
backups folder on the server.

You can't delete the world your server is set to load. The button says so on that row.
Load a different world first — or make a new one — and then delete it.

### Bringing a world over from your own game

Drag the world's folder onto the drop zone (or use **Choose a folder…**).

This is also **the only way to pick your world's seed.** The dedicated server has no
seed option — it just generates a random one the first time it runs. So if you want
a particular seed, create the world in your own copy of Valheim, then upload it here.

On Windows your worlds live in:

```
%USERPROFILE%\AppData\LocalLow\IronGate\Valheim\worlds_local
```

| What to drag in | Notes |
|---|---|
| The world's folder | The normal choice |
| A `.zip` of that folder | 👍 Best for a big, well-explored world — see below |
| A `.db` and `.fwl` pair | An older, pre-1.0 world. Drag both together. |

**Zip it if your world is large.** A modern Valheim world is one small file per patch
of map you've explored, which can run to thousands of files. A `.zip` is a single
file however big the world is. The limits are 1 GB and 5000 files per upload
(`WORLD_UPLOAD_MAX_MB=1024` and `WORLD_UPLOAD_MAX_FILES=5000` in `.env` if you ever
need to raise them), and you'll be told clearly if you hit either.

**Your existing worlds are safe.** An upload that would replace a world already on
the server is refused, and it tells you what's in the way — give it a different name
under *Name on the server* instead. Nothing is saved at all unless the whole world
arrives intact, so a failed upload can't leave you with half a world.

> ⚠️ **Old worlds get converted, and it's one-way.** A pre-1.0 `.db` / `.fwl` pair
> still works, but the first time the server opens it, Valheim rewrites it into the
> new format **permanently**. Keep your own copy of the original pair if that
> matters to you. The dashboard warns you about this too.



---

## 🔒 Keeping it safe

Short version: **this dashboard is powerful, so don't put it on the open internet.**

### Who can reach the dashboard

By default the dashboard listens on every network connection the host machine has —
that's what `MANAGER_BIND=0.0.0.0` means, and it's the default. What it actually
exposes depends entirely on where you're running it:

✅ **At home, behind a normal router** — only devices on your own network can reach
it. That's usually exactly what you want. Just don't forward port 8080 on your
router.

🚨 **On a VPS, cloud server, or anything with a public IP address, `0.0.0.0` means
it is reachable from the entire internet the moment you start it.** Anyone who finds
it gets a login page, and that login can start and stop containers on your machine.
There's no rate limiting on that login, either — see below.

If you're on a VPS, put this in a file called `.env` next to `docker-compose.yml`
before you start:

```bash
MANAGER_BIND=127.0.0.1
```

Now the dashboard only listens to the machine itself, and you reach it through an
SSH tunnel:

```bash
ssh -L 8080:127.0.0.1:8080 you@your-server
```

Then open `http://localhost:8080` on your own computer. A VPN (Tailscale, WireGuard)
or a firewall rule works just as well — the point is that it shouldn't be openly
reachable.

### Use a long admin password

There's **no lockout after failed logins** in this build. That's a deliberate
trade-off for a small tool on a home network, but it means a short password is a
genuinely bad idea. The setup page requires at least 10 characters for this reason.
Longer is better — a passphrase is ideal.

### Signing out doesn't lock out a stolen cookie

Signing out clears the login in *your* browser, but a session copied off your machine
beforehand stays valid until it expires (7 days by default). If that worries you,
shorten `SESSION_MAX_AGE_SECONDS` in `.env` to a few hours.

### The dashboard can't do much to Docker

It has no access to the Docker socket. It talks to a tightly restricted middleman
that only permits the handful of operations this app actually needs — and denies
everything else outright. Details in
[How the Docker access is locked down](#how-the-docker-access-is-locked-down).

---

## 🔧 When something goes wrong

**"Setup link not valid"**
The link is single-use and a restart replaces it. Run `docker compose logs manager`
again and use the newest one. Make sure you copied the whole URL including the
`?token=…` part.

**The setup page says it can't save your settings, or an upload is refused**
Permissions on the folders the dashboard writes to. Re-run `docker compose up -d`,
which runs the helper that fixes them, then check what it said:

```bash
docker compose logs permissions
```

See [How file permissions are handled](#how-file-permissions-are-handled).

**Port 8080 is already in use**
Put `MANAGER_PORT=8099` (or any free number) in a `.env` file next to
`docker-compose.yml`, then `docker compose up -d` again.

**The badge is stuck on "loading world"**
Big worlds take a while. If it never turns green, check the console for errors. If a
game update ever changes the wording the dashboard watches for, you can point it at
the new one with `READY_LOG_PATTERN` in `.env`.

**A container called `valheim-docker-socket-proxy` keeps exiting**
Open `docker-compose.yml` and swap one line on that service:

```yaml
    # cap_drop: [ALL]
    cap_add: [SETUID, SETGID]
```

Some versions of that image need those two permissions to start. Leave
`security_opt: ["no-new-privileges:true"]` exactly as it is.

**You forgot your admin password**
There's no reset page. Delete the login and set it up again — your worlds and
settings are untouched:

```bash
docker compose down
docker volume rm valheim-manager-state
docker compose up -d
docker compose logs manager      # a fresh setup link
```

---

## 🧰 Advanced / Under the hood

Everything below this line is optional. If the server's running and your friends are
in, you can happily ignore all of it.

### What's actually running

Three containers:

| Service | Image | Job |
|---|---|---|
| `valheim` | `ghcr.io/community-valheim-tools/valheim-server` (unmodified) | the game server |
| `manager` | built from `./manager` | the dashboard: FastAPI + a login gate |
| `docker-socket-proxy` | `tecnativa/docker-socket-proxy` | the only container that can see `/var/run/docker.sock` |

`docker compose up -d` deliberately starts only two of them. The game container
doesn't exist until you press **Start** — that's what lets the dashboard report
*downloading → setting up → starting → loading world → ready* as distinct phases
rather than one opaque wait.

### Where everything is stored

None of these files need to exist for the stack to run.

| Location | Holds | Written by |
|---|---|---|
| `valheim-manager-state` volume | admin user, password hash, session secret, mode `0600` | the setup wizard |
| `valheim-config` volume | the worlds (`/config/worlds_local`) and the image's hourly backups | the game server; the dashboard adds uploaded worlds |
| `valheim-data` volume | the game server install itself, downloaded on first Start | the game server |
| `./settings/valheim.env` | game settings: `SERVER_NAME`, `SERVER_PASS`, `WORLD_NAME`, … | created on first boot, then yours |
| `.env` *(optional)* | Compose knobs: ports, image names, log tuning | you |
| `manager.env` *(optional)* | credentials, if you'd rather set them yourself | you |

`.env`, `manager.env` and `settings/` are gitignored.

**`settings/valheim.env` is yours.** The dashboard creates it with sensible defaults
if it's missing, and after that it only ever replaces the *values* of keys you
changed through the UI. Your comments, blank lines, key order, `export` prefixes and
any extra keys you added by hand all survive byte for byte. Every write goes through
a temp file and an atomic rename, so the game server never reads a half-written file.
Drop your own file in before the first `docker compose up -d` and it's left exactly
as you wrote it — `valheim.env.example` is a ready-made starting point.

### How file permissions are handled

Two services write to the same files, as two different users, and neither can hand
itself access. This used to be two `sudo chown` / `chmod` commands in the install
instructions; now it happens on its own.

The problem: Valheim creates `/config/worlds_local` and every world in it as
`PUID:PGID` with a default umask of `022` — mode `755` folders, `644` files. The
dashboard runs as its own uid (`10001`) in Valheim's *group*, and `chmod` requires
being the file's owner or holding `CAP_FOWNER`. The dashboard is neither: it isn't
the owner, and it runs with `cap_drop: [ALL]` on purpose. So it could list your
worlds but not upload beside them, switch away from them, or delete them. Same story
for `settings/`, which Docker creates owned by `root`.

The fix is a one-shot `permissions` container that runs on every `docker compose up`
and exits in about a second. It's root, so it can do what neither long-running
service can, and it has `network_mode: none` — the only root container in the stack
can't talk to anything. It:

- hands `settings/` to the uid the dashboard runs as;
- makes `worlds_local` group-writable and **setgid**, so worlds created later inherit
  the shared group automatically;
- makes the worlds already there group-writable, folders and files.

And `PERMISSIONS_UMASK=002` in `settings/valheim.env` stops the problem recurring at
source — Valheim then creates group-writable files itself, so on later runs the
helper finds nothing to fix.

It's deliberately best-effort and always exits `0`. The dashboard waits for it to
finish, so a non-zero exit would stop the dashboard from starting at all — turning
"uploads are refused" into "nothing runs". If it can't do its job it says so:

```bash
docker compose logs permissions
```

You'll see either `worlds folder ready: ... is 2775` or a `WARNING` line naming what
it found instead.

If you'd rather the dashboard ran as *you*, that works too, but the credential volume
was initialised for uid 10001 and would become unreadable — so it has to go, and
setup runs again:

```bash
docker compose down
docker volume rm valheim-manager-state          # discards the admin account
printf 'MANAGER_UID=%s\nMANAGER_GID=%s\n' "$(id -u)" "$(id -g)" >> .env
docker compose up -d
```

### The shared group (`MANAGER_GID`)

The game server runs as `PUID:PGID` from `settings/valheim.env` (1000 by default) and
saves to its world constantly. The dashboard runs as its own uid and, as above,
cannot `chown` what it writes. A world uploaded under the wrong group would be
readable but not writable — and that shows up much later as the server quietly
failing to save your progress. 😬

So the dashboard runs with the *game's group* and writes uploaded worlds
group-writable:

```yaml
user: "${MANAGER_UID:-10001}:${MANAGER_GID:-1000}"
```

**Keep `MANAGER_GID` equal to `PGID`.** If you change `PGID` in
`settings/valheim.env`, set `MANAGER_GID` to match in `.env`.

The worlds folder is created by the `permissions` helper above if it doesn't exist
yet, and made group-writable if it does, so uploading works on a brand-new volume
without pressing **Start** first. If the dashboard ever does report it can't write
there, re-run `docker compose up -d` — that runs the helper again — and check
`docker compose logs permissions`.

### Deploying on a Linux server (CasaOS, Debian, a NAS)

This stack **builds the dashboard image from source**, so the machine needs the repo
— not just a compose file. That rules out pasting a compose file into CasaOS's
"Custom Install" box, which expects prebuilt images. Clone over SSH instead; CasaOS
will list the running containers afterwards either way.

```bash
sudo apt install -y git
git clone <your-repo-url> valheim-manager
cd valheim-manager
docker compose version                          # needs v2.30.0+
docker compose up -d
docker compose logs manager                     # the one-time setup link
```

If your Compose is older than v2.30.0, either upgrade Docker or delete the four
`env_file:` lines under the `manager` service — that block only exists for the
optional credentials path below.

CasaOS itself uses port 80, so there's no clash with 8080, but set `MANAGER_PORT` in
`.env` if something else has taken it.

### Setting credentials yourself (and the `$` trap)

Setting all three of `ADMIN_USER`, `ADMIN_PASSWORD_HASH` and `SESSION_SECRET` in
`manager.env` skips first-run setup completely: no token is logged and `/setup` never
opens. Useful for scripted deployments or a secrets manager. It's all three or none —
a partial set refuses to boot rather than silently handing you an admin-creation page.

```bash
cp manager.env.example manager.env
docker compose run --rm --no-deps manager python tools/hash_password.py            # bcrypt
docker compose run --rm --no-deps manager python tools/hash_password.py --algo argon2
python -c "import secrets;print(secrets.token_urlsafe(48))"                        # SESSION_SECRET
```

The password is read without echoing and nothing is written to disk.

⚠️ **The `$` trap — this is why the setup wizard exists.** Compose interpolates `$`
in `.env` *and* in a default-format `env_file:`. Every bcrypt hash looks like
`$2b$12$…` and every argon2 hash like `$argon2id$v=19$…`, so `$12`, `$argon2id` and
the digest tail get read as variable references and replaced with empty strings.
`$2b$12$abc…` silently becomes `$2b$12`. Nothing errors — you just get "Invalid
username or password" forever, with no clue why.

The wizard sidesteps this entirely by writing its hash straight to a volume Compose
never reads. The trap only applies to this manual path, where `manager.env` is loaded
with `format: raw` (Compose v2.30.0+), the one loader that passes a value through
untouched. Raw format also takes quotes **literally**, so values there must be bare —
the opposite of the rule in `.env`.

The dashboard recognises a wrecked hash at startup — truncated bcrypt, mangled
argon2, a quoted value — and refuses to boot with an error naming this exact cause,
rather than failing mysteriously at the login screen.

Don't move these three into the `environment:` block in `docker-compose.yml`:
`environment:` overrides `env_file:` for the same key, which reintroduces the bug.

`SESSION_SECRET` must be at least 16 characters and kept stable — it signs the
session cookie, so changing it signs everyone out.

To go back to the wizard, remove the three values **and** the state volume:
`docker volume rm valheim-manager-state`.

### How the Docker access is locked down

The dashboard never sees the Docker socket and never shells out to the `docker` CLI.
It speaks the Engine API over `DOCKER_HOST=tcp://docker-socket-proxy:2375`, and the
proxy's allowlist in `docker-compose.yml` grants only what it actually calls:

```
CONTAINERS  inspect / create / remove / logs
IMAGES      inspect + pull on first run
POST        write methods at all
ALLOW_START / ALLOW_STOP / ALLOW_RESTARTS
```

Everything else — `EXEC`, `BUILD`, `SWARM`, `SECRETS`, `VOLUMES`, `NETWORKS`,
`SYSTEM`, `INFO`, `EVENTS` — is explicitly revoked. The proxy sits on an
`internal: true` network, so nothing on the host or the game network can reach it.

That's still enough to create containers, which is why the dashboard login should be
treated as root-equivalent on that host.

### Other security details

**The login gate.** Nothing behind it leaks: `/` redirects to `/login`, the API
returns `401`, and the log WebSocket is refused before the handshake completes, so an
unauthenticated client never receives a single log line. Wrong credentials give one
generic error with no user enumeration.

**Why setup needs a token.** Because the dashboard login is root-equivalent on the
host, an *open* admin-creation page would hand the service to whoever loaded it first
— a race anyone on the LAN could win. So it needs the one-time token from the log.
The token lives only in memory (a restart mints a new one), is void the moment setup
completes, and a wrong or reused token gets a generic refusal that reveals nothing
and doesn't burn the real one. While unconfigured, every other route redirects to
`/setup`. Anyone who can read `docker compose logs` can complete setup — already true
of anyone who can reach the Docker socket, but treat the log as sensitive until
you're done.

`/healthz` and `/static` are the two exceptions to that redirect. A dashboard waiting
for setup is running *correctly*, so reporting it unhealthy would be a lie: it would
show as `(unhealthy)` in `docker ps`, stall anything gated on
`depends_on: condition: service_healthy`, and send you hunting a fault instead of
reading the setup link.

**Origin check.** Every state-changing POST requires an `Origin` header matching the
server's own host, or one listed in `ALLOWED_ORIGINS`. A request with a valid session
cookie but a foreign `Origin` is rejected before any Docker call — and, for an
upload, before its body is read.

**The game volume.** The dashboard mounts `valheim-config` read-write so the Worlds
panel can see `/config/worlds_local`. It only ever *adds* worlds: there's no delete
path, uploads are refused rather than allowed to overwrite, and every name — typed,
dropped, or read out of an archive — is reduced to a single path segment and
re-resolved against the destination before anything is written. An archive entry that
would land outside its own folder is refused and nothing is written anywhere.

**Passwords at rest.** The state file is written at mode `0600` on a volume no other
service mounts, through a temp file whose mode is set *before* the rename, so the hash
is never briefly world-readable. The password, the hash and the secret are never
logged. With bcrypt only the first 72 bytes count, so both the wizard and the hash
tool refuse anything longer rather than hashing a silently truncated password — argon2
has no such limit and is offered alongside.

**For deliberate remote access**, put it behind a VPN, or behind a reverse proxy with
TLS and set `COOKIE_SECURE=true` plus `ALLOWED_ORIGINS=https://your.host`.

### World modifiers, in full

Valheim's difficulty options live in the settings tab. They're **launch arguments**,
not world data: they change how the server plays and never alter the save you already
have. Like every other setting, they reach the game when you press **Start** after
saving.

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

They all land in one key, `SERVER_ARGS`, and the panel shows you the exact string
before you save. Three rules shape it:

- **Order matters.** `-preset` first, then every `-modifier`, then every `-setkey`.
  Valheim applies these in sequence, so a preset written *after* a category silently
  flattens it back. The dashboard composes the string itself for exactly this reason.
- **Defaults are omissions.** Every category has a `normal` setting, which is what you
  get when the argument is absent — and which Valheim doesn't document as a value. So
  a row left on default writes nothing at all, rather than `-modifier combat normal`.
- **Your own arguments survive.** Anything already in `SERVER_ARGS` that isn't one of
  the above is kept exactly as you wrote it and moved after them. Changing a dropdown
  never costs you a flag you added by hand.

### Editing `settings/valheim.env` by hand

Perfectly supported, including for keys the UI doesn't offer. The change still has to
reach a *new* container, which is the three steps the UI automates for you:

1. Edit `settings/valheim.env`.
2. Press **Stop** and wait for the badge to read **off**.
3. Remove the old container, then press **Start**:

```bash
docker rm valheim-server
```

That third step is the one people miss. A container keeps the settings it was created
with, so Start or Restart on their own would quietly run your old values. (`docker rm`
refuses a running container, which is why step 2 has to finish first.)

This is also exactly what **Save** does for you: it validates, rewrites the file
atomically, and removes the stopped container. A save that changes nothing writes
nothing.

`WORLD_NAME` is the one field to think twice about. It picks which save to load, so
pointing it at a name that doesn't exist yet generates a **brand-new world** with a
new random seed rather than renaming your old one. (Your old world stays put.) Use
the Worlds tab to move between worlds that already exist — it only offers real ones.

### Running the game container without the dashboard

The dashboard owns the game container, but `docker-compose.yml` also defines the
`valheim` service behind a profile, as both documentation and an escape hatch:

```bash
docker compose --profile server up -d valheim   # bypass the dashboard
```

Both paths share the container name and volumes, so use one or the other, not both.
Published ports are the one place they can drift: the dashboard derives them from
`SERVER_PORT` in `settings/valheim.env`, the profile path uses `VALHEIM_PORTS` in
`.env`.

### Running the tests

```bash
docker compose config -q                 # valid with no env files present
docker compose --profile server config   # ... including the game service

python -m pip install -r manager/requirements-dev.txt

# Part of the suite runs the dashboard's app.js in a real DOM rather than reading it
# as text -- which panel is showing, whether the console kept its buffer, whether a
# lock reached the tab strip are runtime facts a source-text check cannot see. Needs
# node on PATH, once. These tests FAIL rather than skip without it: they are the only
# checks standing behind the tab strip's behaviour.
cd manager/app/tests/js && npm install && cd ../../../..

cd manager && python -m pytest
```

A manual pass, from a tree with no `.env`, no `manager.env` and no `settings/`:

1. `docker compose up -d`, complete the wizard at the logged URL, confirm you land
   signed in without a second login.
2. Confirm the credential file is `0600` and holds no plaintext password:
   `docker compose exec manager ls -l /srv/state/manager-state.json`
3. Reload `/setup` with the same token — it must redirect to `/login`, and nothing
   may be overwritten.
4. `docker compose restart manager` — the session survives and setup stays closed.
5. Before pressing Start: `docker ps -a` shows no game container and
   `docker run --rm -v valheim-config:/c busybox ls /c/worlds_local` is empty.
6. **Start** → watch *downloading → setting up → starting → loading world → ready*,
   then **Stop**, then **Start** again.
7. Compare the console against `docker compose logs -f valheim`.
8. Connect a Valheim client to `<host>:2456` once the badge says **ready**.
9. With the server stopped, add a flag of your own to `SERVER_ARGS` by hand, then
   press **Edit**, pick a preset and a couple of modifiers, and confirm the previewed
   string is what lands in the file — with your flag still there, after the modifiers.

### Project layout

```
docker-compose.yml            three services, scoped proxy allowlist
manager.env.example           OPTIONAL credential override (copy to manager.env)
valheim.env.example           reference copy of the game settings
settings/valheim.env          created on first boot; yours to edit afterwards
manager/
  Dockerfile                  python:3.12-slim, runs as uid 10001
  .env.example                optional Compose-level knobs (copy to ./.env)
  requirements.txt            runtime dependencies, the only ones in the image
  requirements-dev.txt        those plus pytest, for running the suite
  tools/hash_password.py      ADMIN_PASSWORD_HASH generator (advanced path only)
  app/
    main.py                   FastAPI routes, credential resolution, /ws/logs
    setup.py                  first-run token, shared field validation, the write
    state_store.py            the 0600 credential state file
    auth.py                   bcrypt/argon2 + signed SameSite=Strict cookie
    docker_control.py         Engine API via the proxy; readiness detection
    settings_store.py         atomic, comment-preserving writes to valheim.env
    modifiers.py              the world-modifier vocabulary, to and from SERVER_ARGS
    worlds.py                 world listing, upload validation, staged placement
    templates/ static/        server-rendered HTML + vanilla JS, no build step
    tests/                    the suite, including the jsdom harness
```

### Not built yet

Browsing and restoring backups from the dashboard, and admin/ban-list editing. The
game image keeps taking its own hourly world backups into `/config/backups`
alongside any you take by hand.

---

Requires Linux containers — a native Linux host, or Docker Desktop on WSL2.
Windows-container / Hyper-V mode is not supported.
