"""Per-world mods, and getting the right set of them in front of the game server.

Valheim's modding framework is BepInEx, and BepInEx has exactly one plugins folder.
It has no concept of "the mods for this world" -- so per-world mods are this
manager's job, not the framework's: mods are stored per world here, and the set
belonging to the world the server is about to load is copied into the plugins folder
before the container is created.

Three things shape this module.

**The image's own copy does not delete.** `valheim-server-docker` rsyncs
``/config/bepinex/plugins/`` into the game install with ``rsync -a`` and no
``--delete``, so a mod removed from the staging folder stays in the install, and the
install lives on a volume that survives the container. Switching from a modded world
to an unmodded one would therefore leave the old mods loaded, and the per-world
promise would be a lie. The manager writes both folders itself.

**Only what we put there.** BepInEx may keep files of its own in the plugins folder,
and the operator may have put something there by hand before this feature existed.
So a sync never empties the directory: it reads ``MANIFEST``, removes exactly the
entries it placed last time, and writes the new set. Anything else is left alone.

**Disabled is a rename, not a deletion.** A mod that breaks the server has to be
switchable off without losing the file -- that is how you find the one that broke it.
A disabled mod keeps its bytes under a ``.off`` suffix and is simply not synced.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Sequence

from .worlds import (
    WORLD_DIR_MODE,
    WORLD_DIR_MODE_FALLBACK,
    WORLD_FILE_MODE,
    Member,
    WorldError,
    _resolved_within,
    _set_mode,
    human_size,
    sanitised_name,
)

log = logging.getLogger("valheim_manager")

DEFAULT_MODS_DIR = "/config/mods"
# Where the image expects operator-supplied plugins, and where it copies them to.
DEFAULT_STAGING_PLUGINS_DIR = "/config/bepinex/plugins"
DEFAULT_GAME_PLUGINS_DIR = "/opt/valheim/bepinex/BepInEx/plugins"

# A disabled mod keeps its bytes and loses its place in the sync.
DISABLED_SUFFIX = ".off"

# The record of what this manager last placed in a plugins folder. Without it a sync
# would have to empty the directory, which would take BepInEx's own files with it.
MANIFEST = ".valheim-manager-mods"

# What a mod can be dropped as. A folder is one too -- plenty of mods ship a dll
# beside a config or assets directory.
MOD_FILE_SUFFIXES = (".dll",)

DEFAULT_MAX_MOD_MB = 256
DEFAULT_MAX_MOD_FILES = 2000


class ModError(WorldError):
    """A mod operation was refused. The message is safe to show the operator."""


@dataclass(frozen=True)
class Mod:
    """One mod belonging to one world."""

    name: str
    enabled: bool
    size_bytes: int
    file_count: int
    is_directory: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "size_bytes": self.size_bytes,
            "size": human_size(self.size_bytes),
            "files": self.file_count,
            "directory": self.is_directory,
        }


def _entry_name(name: str, *, enabled: bool) -> str:
    return name if enabled else name + DISABLED_SUFFIX


def _display_name(entry: str) -> tuple[str, bool]:
    """``(name, enabled)`` for an on-disk entry."""
    if entry.endswith(DISABLED_SUFFIX):
        return entry[: -len(DISABLED_SUFFIX)], False
    return entry, True


def _measure(path: Path) -> tuple[int, int]:
    """``(bytes, files)`` for a file or a directory tree."""
    if path.is_file():
        try:
            return path.stat().st_size, 1
        except OSError:  # pragma: no cover - removed mid-scan
            return 0, 0
    total = 0
    count = 0
    for child in path.rglob("*"):
        try:
            if child.is_file(follow_symlinks=False):
                total += child.stat(follow_symlinks=False).st_size
                count += 1
        except OSError:  # pragma: no cover - removed mid-scan
            continue
    return total, count


class ModStore:
    """Mods on the game volume, stored per world and synced into BepInEx."""

    def __init__(
        self,
        mods_dir: str | os.PathLike[str] = DEFAULT_MODS_DIR,
        *,
        staging_plugins_dir: str | os.PathLike[str] = DEFAULT_STAGING_PLUGINS_DIR,
        game_plugins_dir: str | os.PathLike[str] = DEFAULT_GAME_PLUGINS_DIR,
        max_upload_bytes: int = DEFAULT_MAX_MOD_MB * 1024 * 1024,
        max_upload_files: int = DEFAULT_MAX_MOD_FILES,
    ):
        self.root = Path(mods_dir)
        # Two, and both matter: the first is what the image copies from, the second is
        # what the game actually loads. Writing only the first leaves a removed mod
        # live, because the image's copy has no --delete.
        self.staging_plugins_dir = Path(staging_plugins_dir)
        self.game_plugins_dir = Path(game_plugins_dir)
        self.max_upload_bytes = max_upload_bytes
        self.max_upload_files = max_upload_files

    # ------------------------------------------------------------------ listing

    def world_dir(self, world: str) -> Path:
        """The folder holding one world's mods. The world name is validated first."""
        return self.root / sanitised_name(world)

    def mods(self, world: str) -> list[Mod]:
        """Every mod belonging to ``world``, enabled and disabled, sorted by name."""
        directory = self.world_dir(world)
        if not directory.is_dir():
            return []
        found: list[Mod] = []
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.lower())
        except OSError as exc:
            raise ModError(f"Could not read the mods folder ({exc}).") from exc
        for entry in entries:
            if entry.name == MANIFEST or entry.name.startswith("."):
                continue
            name, enabled = _display_name(entry.name)
            size, count = _measure(Path(entry.path))
            found.append(
                Mod(
                    name=name,
                    enabled=enabled,
                    size_bytes=size,
                    file_count=count,
                    is_directory=entry.is_dir(),
                )
            )
        return found

    def enabled_mods(self, world: str) -> list[Mod]:
        return [mod for mod in self.mods(world) if mod.enabled]

    def has_enabled(self, world: str) -> bool:
        """Whether the game should be started with BepInEx on for this world."""
        try:
            return bool(self.enabled_mods(world))
        except (ModError, WorldError):
            # A mods folder we cannot read is not a reason to refuse to start the
            # server; it means we cannot claim there are mods to load.
            return False

    # ------------------------------------------------------------------ writing

    def _ensure(self, directory: Path, *, what: str) -> None:
        if directory.is_dir():
            return
        if directory.exists():
            raise ModError(f"{directory} exists but is not a directory.")
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ModError(
                f"Could not open the {what} ({exc}). Run `docker compose up -d` on the "
                "server -- that re-runs the one-off step that grants the manager "
                "access -- then try again. Nothing was saved."
            ) from exc
        _set_mode(directory, WORLD_DIR_MODE, fallback=WORLD_DIR_MODE_FALLBACK)

    def _existing_entry(self, world: str, name: str) -> Path:
        """The on-disk path of ``name`` in ``world``, enabled or not. Raises if absent."""
        safe = sanitised_name(name, what="mod name")
        directory = self.world_dir(world)
        for candidate in (safe, safe + DISABLED_SUFFIX):
            path = _resolved_within(directory, candidate)
            if path.exists():
                return path
        raise ModError(
            f"There is no mod called {safe!r} for this world. Nothing was changed -- "
            "refresh the list."
        )

    def place(self, world: str, members: Sequence[Member], requested_name: str = "") -> Mod:
        """Store an uploaded mod for ``world``.

        A single ``.dll`` lands as that file. Anything else lands as a folder named
        after the upload -- which is how a Thunderstore archive, a dll beside its
        config, and a folder drop all end up as one entry the operator can switch off
        in one press.
        """
        if not members:
            raise ModError(
                "Nothing arrived. Drop a mod's .dll, or the .zip you downloaded."
            )
        if len(members) > self.max_upload_files:
            raise ModError(
                f"That upload holds {len(members)} files and at most "
                f"{self.max_upload_files} can go at once. Nothing was saved."
            )
        total = sum(member.size for member in members)
        if total > self.max_upload_bytes:
            raise ModError(
                f"That upload is {human_size(total)}, over the "
                f"{human_size(self.max_upload_bytes)} limit for one mod. Nothing was "
                "saved."
            )

        single = _single_dll(members)
        name = sanitised_name(
            requested_name or (single.leaf if single else _suggested_name(members)),
            what="mod name",
        )
        if single and not name.lower().endswith(".dll"):
            name += ".dll"

        directory = self.world_dir(world)
        self._ensure(directory, what="mods folder")
        for existing in (name, name + DISABLED_SUFFIX):
            if _resolved_within(directory, existing).exists():
                raise ModError(
                    f"This world already has a mod called {name}. Delete it first, or "
                    "upload it under a different name. Nothing was saved."
                )

        staging = _resolved_within(directory, "." + name + ".part")
        _remove(staging)
        try:
            if single:
                _write_file(single, staging)
            else:
                staging.mkdir(parents=True)
                _set_mode(staging, WORLD_DIR_MODE, fallback=WORLD_DIR_MODE_FALLBACK)
                for member in members:
                    destination = _resolved_within(staging, member.path)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _set_mode(
                        destination.parent, WORLD_DIR_MODE, fallback=WORLD_DIR_MODE_FALLBACK
                    )
                    _write_file(member, destination)
        except Exception:
            _remove(staging)
            raise

        target = _resolved_within(directory, name)
        os.replace(staging, target)
        size, count = _measure(target)
        log.info("Mod %r uploaded for world %r (%s).", name, world, human_size(size))
        return Mod(
            name=name,
            enabled=True,
            size_bytes=size,
            file_count=count,
            is_directory=target.is_dir(),
        )

    def delete(self, world: str, name: str) -> str:
        """Remove a mod from a world entirely. Returns the name that went."""
        path = self._existing_entry(world, name)
        if path.is_symlink():
            raise ModError(
                f"{path.name!r} is a link, not a mod, so the manager will not delete "
                "it. Remove it on the host if you meant to."
            )
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            raise ModError(f"Could not delete that mod ({exc}). Nothing was changed.") from exc
        shown, _ = _display_name(path.name)
        log.info("Mod %r deleted from world %r.", shown, world)
        return shown

    def set_enabled(self, world: str, name: str, enabled: bool) -> Mod:
        """Switch a mod on or off. The bytes stay either way."""
        path = self._existing_entry(world, name)
        shown, currently = _display_name(path.name)
        if currently == enabled:
            size, count = _measure(path)
            return Mod(
                name=shown,
                enabled=enabled,
                size_bytes=size,
                file_count=count,
                is_directory=path.is_dir(),
            )
        target = path.with_name(_entry_name(shown, enabled=enabled))
        if target.exists():
            raise ModError(
                f"There is already something called {target.name!r} in this world's "
                "mods. Nothing was changed."
            )
        try:
            os.replace(path, target)
        except OSError as exc:
            raise ModError(
                f"Could not switch that mod {'on' if enabled else 'off'} ({exc}). "
                "Nothing was changed."
            ) from exc
        log.info(
            "Mod %r %s for world %r.", shown, "enabled" if enabled else "disabled", world
        )
        size, count = _measure(target)
        return Mod(
            name=shown,
            enabled=enabled,
            size_bytes=size,
            file_count=count,
            is_directory=target.is_dir(),
        )

    # -------------------------------------------------------------------- sync

    def sync(self, world: str) -> list[str]:
        """Put ``world``'s enabled mods in front of the game. Returns what was placed.

        Called before the container is created, so the plugins folder already holds
        the right set when BepInEx starts reading it. Both plugin folders are written:
        the staging one the image copies from, and the install one it copies to --
        whose copy has no ``--delete``, so a removal that only touched staging would
        never reach the game.
        """
        wanted = {mod.name: self.world_dir(world) / mod.name for mod in self.enabled_mods(world)}
        placed: list[str] = []
        for plugins in (self.staging_plugins_dir, self.game_plugins_dir):
            if plugins is self.staging_plugins_dir:
                # The image creates this on its first BepInEx install; the manager
                # creates it early so a mod can be staged before that ever happens.
                try:
                    self._ensure(plugins, what="plugins folder")
                except ModError as exc:
                    log.warning("Could not prepare %s: %s", plugins, exc)
                    continue
            elif not plugins.is_dir():
                # BepInEx is not installed yet. The image will install it on this
                # start and copy the staging folder in, so there is nothing to clear.
                continue
            self._clear_managed(plugins)
            names: list[str] = []
            for name, source in wanted.items():
                destination = _resolved_within(plugins, name)
                try:
                    if source.is_dir():
                        shutil.copytree(source, destination, dirs_exist_ok=True)
                    else:
                        shutil.copy2(source, destination)
                except OSError as exc:
                    log.warning("Could not place mod %r in %s: %s", name, plugins, exc)
                    continue
                names.append(name)
            self._write_manifest(plugins, names)
            placed = names
        log.info("Synced %d mod(s) for world %r.", len(placed), world)
        return placed

    def _clear_managed(self, plugins: Path) -> None:
        """Remove exactly what this manager placed here last time, and nothing else."""
        for name in self._read_manifest(plugins):
            try:
                path = _resolved_within(plugins, name)
            except WorldError:  # pragma: no cover - a manifest we did not write
                continue
            if path.is_symlink() or not path.exists():
                continue
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError as exc:  # pragma: no cover - reported, not fatal
                log.warning("Could not remove stale mod %r from %s: %s", name, plugins, exc)

    def _read_manifest(self, plugins: Path) -> list[str]:
        try:
            raw = (plugins / MANIFEST).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def _write_manifest(self, plugins: Path, names: Sequence[str]) -> None:
        path = plugins / MANIFEST
        try:
            path.write_text("".join(f"{name}\n" for name in names), encoding="utf-8")
            _set_mode(path, WORLD_FILE_MODE)
        except OSError as exc:  # pragma: no cover - reported, not fatal
            log.warning("Could not record which mods were placed in %s: %s", plugins, exc)


def _single_dll(members: Sequence[Member]) -> Member | None:
    """The one member, when the upload is a single loose ``.dll``."""
    if len(members) != 1:
        return None
    member = members[0]
    return member if member.leaf.lower().endswith(MOD_FILE_SUFFIXES) else None


def _suggested_name(members: Sequence[Member]) -> str:
    """A name for a multi-file mod: its own top folder, or its first dll's stem."""
    roots = {member.path.split("/", 1)[0] for member in members if "/" in member.path}
    if len(roots) == 1:
        return roots.pop()
    for member in members:
        if member.leaf.lower().endswith(MOD_FILE_SUFFIXES):
            return member.leaf[: -len(".dll")]
    raise ModError(
        "That upload does not look like a mod: no .dll in it, and no single folder to "
        "name it after. Nothing was saved."
    )


def _write_file(member: Member, destination: Path) -> None:
    with member.open() as source, open(destination, "wb") as handle:
        shutil.copyfileobj(source, handle)
    _set_mode(destination, WORLD_FILE_MODE)


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
