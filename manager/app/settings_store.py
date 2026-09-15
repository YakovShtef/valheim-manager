"""Read and write the Valheim settings env file.

The compose stack feeds the very same file to the ``valheim`` service via
``env_file:``, and the manager reads it both to *display* current values and to
build the container environment when it has to create the container itself on
first run.

Writing is deliberately narrow. ``ensure_default`` creates the file with documented
defaults when -- and only when -- none exists, so a clean ``docker compose up -d``
has something to show; ``write`` replaces the values of named keys in place, leaving
comments, blank lines, key order and unrecognised keys exactly as they were. Both go
through a temp file and ``os.replace``, so the ``valheim`` service never reads a
half-written file.

Two writers: the first-run setup wizard, and the dashboard's settings panel (which
may only save while the server is off, and removes the stopped container so the next
Start picks the new values up). Valheim's world modifiers are just another key on that
path -- the panel composes ``SERVER_ARGS`` from its modifier controls (see
``modifiers``) and saves it through this same writer.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import tempfile
from pathlib import Path

# The same durability helper both atomic writers need; it lives beside the credential
# store because that is where the promise it keeps is strongest.
from .state_store import fsync_directory

log = logging.getLogger(__name__)

# Keys whose values must never be rendered into the WebUI.
SECRET_KEY_PATTERN = re.compile(r"(PASS|PASSWORD|SECRET|TOKEN|APIKEY|API_KEY)", re.IGNORECASE)

# Keys surfaced first in the UI's "current settings" panel; anything else in the
# file is still shown, just after these.
PRIMARY_KEYS = (
    "SERVER_NAME",
    "WORLD_NAME",
    "SERVER_PORT",
    "SERVER_PASS",
    "SERVER_PUBLIC",
    "CROSSPLAY",
    "SERVER_ARGS",
)

# What each key is called in the panel. The editor has always used plain names on its
# own labels; the read-only table showed the raw key beside them, so the same setting
# had two names depending on whether you were looking at it or changing it. A key with
# no entry here -- anything the operator added to the file themselves -- keeps its own
# name, which is the only name it has.
SETTINGS_LABELS = {
    "SERVER_NAME": "Server name",
    "WORLD_NAME": "World name",
    "SERVER_PORT": "Game port",
    "SERVER_PASS": "Join password",
    "SERVER_PUBLIC": "In the public server list",
    "CROSSPLAY": "Crossplay (Xbox / Game Pass)",
    "SERVER_ARGS": "World modifiers",
    "TZ": "Time zone",
    "BACKUPS": "Automatic backups",
    "BACKUPS_INTERVAL": "Backup every (seconds)",
    "BACKUPS_MAX_AGE": "Keep backups for (days)",
    "UPDATE_ON_START": "Update the game on start",
}

_LINE_RE = re.compile(
    r"""^\s*(?:export\s+)?          # optional `export ` prefix
        (?P<key>[A-Za-z_][A-Za-z0-9_]*)
        \s*=\s*
        (?P<value>.*?)\s*$""",
    re.VERBOSE,
)

# Same shape, but keeping every byte we did not come to change: the leading
# whitespace and `export `, the spacing around `=`, and the raw right-hand side.
_WRITE_LINE_RE = re.compile(
    r"""^(?P<prefix>\s*(?:export\s+)?)
        (?P<key>[A-Za-z_][A-Za-z0-9_]*)
        (?P<eq>\s*=\s*)
        (?P<raw>.*)$""",
    re.VERBOSE,
)

# A value carrying any of these cannot be written bare: whitespace would be
# stripped, `#` would start an inline comment, and a quote would be read as one.
_NEEDS_QUOTING_RE = re.compile(r"[\s#'\"]")

MASK = "********"


def is_secret_key(key: str) -> bool:
    """True for a key whose value must never be rendered into the WebUI."""
    return bool(SECRET_KEY_PATTERN.search(key))


def is_untouched_secret(key: str, value: str) -> bool:
    """True when ``value`` is the mask this store rendered for ``key``.

    The settings panel prefills secrets with ``MASK`` rather than the real value, so a
    form submitted without retyping one posts the mask straight back. Answering "that
    is the mask, not a value" here is what stops ``********`` being written as the join
    password -- which would lock every player out with a value that still looks
    plausible in the panel. It also means the mask can never *be* a stored secret,
    which is a trade the alternative (echoing the real password into the page) does not
    come close to earning.
    """
    return is_secret_key(key) and value == MASK

# Written verbatim on a clean install. Kept in step with valheim.env.example by
# ``test_default_settings_text_matches_the_shipped_example``; the example stays in
# the repo for operators who want the advanced, hand-edited path, but the manager
# cannot read it (the image ships only app/ and tools/), so the text lives here.
DEFAULT_SETTINGS_TEXT = """\
# ---------------------------------------------------------------------------
# Valheim server settings, created by the manager on first boot.
#
# Owned by you from here on: the manager never overwrites this file, only the
# values of the keys it was asked to change (comments, ordering and any extra
# keys you add are preserved). Every variable below is documented by the
# upstream image:
# https://github.com/community-valheim-tools/valheim-server-docker
#
# To apply a change: edit here, then Stop -> `docker rm valheim-server` -> Start.
# ---------------------------------------------------------------------------

# Name shown in the in-game server browser.
SERVER_NAME=My Valheim Server

# World save name under /config/worlds_local. The world itself is created by the
# game server the first time you press Start.
WORLD_NAME=Dedicated

# UDP game port. The query port (+1) and crossplay backend port (+2) follow it.
SERVER_PORT=2456

# Join password. REQUIRED, at least 5 characters: valheim_server.x86_64 itself
# refuses to start without one, whatever SERVER_PUBLIC says, and there is no
# passwordless option. Left empty here so an unedited copy of this file cannot put a
# server online under a password published in a repo -- the setup wizard asks you for
# one, and Start refuses while this is empty rather than leaving you with a container
# that exits on its own a second later.
SERVER_PASS=

# List the server in the public community browser (1/true) or keep it
# join-by-address only (0/false). Going public is an opt-in decision.
SERVER_PUBLIC=0

# Allow Xbox/Game Pass clients. Uses the PlayFab backend and publishes a join
# code instead of an address.
CROSSPLAY=false

# Extra command-line arguments for the game server, which is where Valheim's world
# modifiers live. The settings panel writes this line for you: a preset, the
# combat/deathpenalty/resources/raids/portals categories, and the nobuildcost,
# playerevents, passivemobs and nomap toggles -- composed as
# `-preset <name> -modifier <category> <value>... -setkey <name>...`, in that order,
# because Valheim applies these in sequence and a preset resets whatever came before
# it. A category left at its default is left out entirely; `normal` is not one of the
# documented argument values. Any other argument you put here is kept exactly as you
# wrote it and moved after the modifiers.
#
# These are launch arguments, not world data: they change the rules the server plays
# by and never alter or migrate an existing save. They take effect when a new
# container is created, which is what pressing Save then Start does.
SERVER_ARGS=

# Run the server process as this uid/gid instead of root.
PUID=1000
PGID=1000

# Create worlds, backups and config group-writable (775 / 664) instead of the
# image's default 755 / 644. The manager runs as its own uid in this same group,
# and cannot chmod what the game server owns -- so without this, every world the
# game creates is one the manager can list but not upload beside, switch away
# from, or delete. Keep this at 002 unless you know you want otherwise.
PERMISSIONS_UMASK=002

TZ=Etc/UTC

# Keep the game files up to date on container start.
UPDATE_ON_START=true

# The image's own hourly world backup under /config/backups.
BACKUPS=true
BACKUPS_INTERVAL=3600
BACKUPS_MAX_AGE=3
"""

# A file the operator may want to read on the host; the join password in it is a
# game credential, not a manager one (those live in state_store at 0600).
DEFAULT_SETTINGS_MODE = 0o644


class SettingsFileError(RuntimeError):
    """The settings env file is missing, unreadable, or cannot be written."""


def _not_utf8(path: Path, exc: UnicodeDecodeError) -> SettingsFileError:
    """This is the one file the design invites the operator to edit on the host, and
    that host is often Windows: Notepad's "Unicode" is UTF-16, and a single accented
    character typed in a legacy editor is latin-1. Either would otherwise escape as a
    raw ``UnicodeDecodeError``, 500 the dashboard and kill the log pump."""
    return SettingsFileError(
        f"{path} is not valid UTF-8 ({exc.reason} at byte {exc.start}). Docker reads "
        "env files as UTF-8, so save it that way -- in Notepad choose "
        '"UTF-8", not "Unicode", and avoid "ANSI".'
    )


def _split_raw_value(raw: str) -> tuple[str, str]:
    """Split a raw right-hand side into (value as written, trailing comment).

    The two halves concatenate back to ``raw``, which is what lets a rewrite keep an
    inline comment byte for byte. One rule, shared by the reader and the writer, so
    they cannot disagree: a quoted value owns every ``#`` inside its quotes, and after
    the closing quote -- or anywhere in an unquoted value -- ``␣#`` starts a comment.

    Deriving this from "is the *whole* value quoted?" was wrong: it left the quotes in
    the value whenever a quoted value was followed by an inline comment, so
    ``NAME="My Server" # note`` read back as ``"My Server"``, quotes included -- and a
    value the writer had to quote onto such a line did not survive a round trip.
    """
    if raw[:1] in ("'", '"'):
        quote = raw[0]
        end = raw.find(quote, 1)
        if end != -1:
            return raw[: end + 1], raw[end + 1 :]
        return raw, ""  # unterminated quote: not ours to guess at
    match = re.search(r"\s+#.*$", raw)
    if match:
        return raw[: match.start()], raw[match.start() :]
    return raw, ""


def _unquote(raw: str) -> str:
    """Strip one layer of matching quotes and any trailing inline comment."""
    value, _ = _split_raw_value(raw)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value.strip()


def format_env_value(key: str, value: str) -> str:
    """Render ``value`` so that ``parse_env_text`` reads it back unchanged."""
    if "\n" in value or "\r" in value:
        raise SettingsFileError(f"{key} cannot contain a line break.")
    if value == "" or not _NEEDS_QUOTING_RE.search(value):
        return value
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    raise SettingsFileError(
        f"{key} cannot contain both a single and a double quote: an env file offers no "
        "escape for the quote that wraps the value, so it could not be read back."
    )


def parse_env_text(text: str) -> dict[str, str]:
    """Parse env-file text into an ordered mapping. Later keys win, as Docker does."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if not match:
            continue
        values[match.group("key")] = _unquote(match.group("value"))
    return values


def render_env_text(text: str, updates: dict[str, str]) -> str:
    """Return ``text`` with the values of ``updates``' keys replaced.

    Everything else survives byte for byte: comment lines, blank lines, the
    original key order, ``export `` prefixes, spacing around ``=``, inline
    comments, and any key the manager knows nothing about. Keys not already in the
    file are appended. A key that appears more than once is updated at every
    occurrence, so the last-wins value Docker would use is the new one.

    One thing is deliberately not preserved: CRLF line endings become LF. A stray
    ``\\r`` in an env file ends up *inside* the value the container receives, so a CRLF
    file is already broken for the game server and normalising it is the lesser
    surprise. A file nobody asked to change is never rewritten at all --
    ``ensure_default`` uses ``O_EXCL`` -- so this only applies to a requested edit.
    """
    lines = text.splitlines()
    trailing_newline = text.endswith(("\n", "\r"))
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        match = _WRITE_LINE_RE.match(line)
        if match is None or line.lstrip().startswith("#") or match.group("key") not in updates:
            out.append(line)
            continue
        key = match.group("key")
        _, comment = _split_raw_value(match.group("raw"))
        rendered_value = format_env_value(key, updates[key])
        if comment and not rendered_value:
            # `KEY= # note` would read back as the comment text. Two quotes say
            # "deliberately empty" and survive the round trip.
            rendered_value = '""'
        out.append(
            match.group("prefix") + key + match.group("eq") + rendered_value + comment
        )
        seen.add(key)

    appended = [key for key in updates if key not in seen]
    if appended:
        if out and out[-1].strip():
            out.append("")
        out.append("# Added by the Valheim manager.")
        out += [f"{key}={format_env_value(key, updates[key])}" for key in appended]
        trailing_newline = True

    rendered = "\n".join(out)
    return rendered + "\n" if trailing_newline else rendered


class SettingsStore:
    """Reads the Valheim env file on demand (no caching -- the file is tiny)."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    # ------------------------------------------------------------------ read

    def _reject_directory(self) -> None:
        if self.path.is_dir():
            raise SettingsFileError(
                f"{self.path} is a directory, not a file. The manager expects the "
                "settings *directory* to be mounted and to own valheim.env inside it; "
                "a directory at this path means something created it by hand. Remove it "
                "on the host and restart the manager, which will write a default "
                "valheim.env (see valheim.env.example for the documented values)."
            )

    def read(self) -> dict[str, str]:
        self._reject_directory()
        try:
            text = self.path.read_text(encoding="utf-8")
        except IsADirectoryError as exc:  # pragma: no cover - covered by is_dir above
            raise SettingsFileError(f"{self.path} is a directory, not a file.") from exc
        except FileNotFoundError as exc:
            raise SettingsFileError(
                f"Settings env file not found at {self.path}. The manager creates it on "
                "boot; if it is missing, the mounted settings directory is not writable "
                "by the manager's uid."
            ) from exc
        except UnicodeDecodeError as exc:
            raise _not_utf8(self.path, exc) from exc
        except OSError as exc:  # pragma: no cover - unreadable file
            raise SettingsFileError(f"Could not read {self.path}: {exc}") from exc
        return parse_env_text(text)

    def container_env(self) -> dict[str, str]:
        """Exactly what should be handed to the Valheim container on create."""
        return self.read()

    def display_settings(self) -> list[dict[str, str | bool]]:
        """Settings for the UI panel, secrets masked, primary keys first."""
        values = self.read()
        ordered: list[str] = [k for k in PRIMARY_KEYS if k in values]
        ordered += [k for k in values if k not in ordered]
        rows: list[dict[str, str | bool]] = []
        for key in ordered:
            secret = is_secret_key(key)
            raw = values[key]
            rows.append(
                {
                    "key": key,
                    "label": SETTINGS_LABELS.get(key, key),
                    "value": MASK if (secret and raw) else raw,
                    "secret": secret,
                }
            )
        return rows

    def server_port(self, default: int = 2456) -> int:
        """The game port. Also implies the query (+1) and crossplay (+2) ports, so the
        usable range stops at 65533."""
        try:
            raw = self.read().get("SERVER_PORT", "") or default
            port = int(raw)
        except (SettingsFileError, ValueError):
            return default
        if not 1 <= port <= 65533:
            log.warning(
                "SERVER_PORT=%s is outside 1-65533 (it needs two ports above it); "
                "using %s",
                port,
                default,
            )
            return default
        return port

    # ----------------------------------------------------------------- write

    def ensure_default(self) -> bool:
        """Create the file with documented defaults if it does not exist.

        Returns ``True`` only when this call created it. An operator's own file is
        never read, rewritten or reformatted here -- ``O_EXCL`` means even a racing
        second manager cannot clobber it.
        """
        self._reject_directory()
        parent = self.path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SettingsFileError(
                f"The settings directory {parent} cannot be created: {exc}"
            ) from exc
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, DEFAULT_SETTINGS_MODE)
        except FileExistsError:
            return False
        except OSError as exc:
            raise SettingsFileError(
                f"Could not create {self.path}: {exc}. The mounted settings directory "
                "must be writable by the manager's uid."
            ) from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(DEFAULT_SETTINGS_TEXT)
        return True

    def write(self, updates: dict[str, str]) -> None:
        """Replace the values of ``updates``' keys, atomically and in place.

        Temp file in the same directory then ``os.replace``, so a reader (the
        ``valheim`` service's ``env_file:``) sees either the old file or the new one
        and never a truncated one. The existing file's mode is preserved.
        """
        if not updates:
            return
        self._reject_directory()
        parent = self.path.parent
        try:
            original = self.path.read_text(encoding="utf-8")
            mode = stat.S_IMODE(self.path.stat().st_mode)
        except FileNotFoundError:
            # Boot could not create it (unwritable directory, then fixed): fall back
            # to the documented defaults rather than writing a bare key=value file.
            original = DEFAULT_SETTINGS_TEXT
            mode = DEFAULT_SETTINGS_MODE
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise SettingsFileError(
                    f"The settings directory {parent} cannot be created: {exc}"
                ) from exc
        except UnicodeDecodeError as exc:
            raise _not_utf8(self.path, exc) from exc
        except OSError as exc:
            raise SettingsFileError(f"Could not read {self.path}: {exc}") from exc

        rendered = render_env_text(original, updates)
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(parent), prefix=".valheim-env-", suffix=".tmp"
            )
        except OSError as exc:
            raise SettingsFileError(
                f"Could not write to the settings directory {parent}: {exc}. It must be "
                "writable by the manager's uid."
            ) from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, mode)
            os.replace(tmp_name, self.path)
            fsync_directory(parent)
        except OSError as exc:
            try:
                os.unlink(tmp_name)
            except OSError:  # pragma: no cover - the temp file may already be gone
                pass
            raise SettingsFileError(f"Could not write {self.path}: {exc}") from exc


__all__ = [
    "DEFAULT_SETTINGS_TEXT",
    "MASK",
    "SettingsFileError",
    "SettingsStore",
    "format_env_value",
    "is_secret_key",
    "is_untouched_secret",
    "parse_env_text",
    "render_env_text",
]
