"""Valheim's world modifiers: the vocabulary, the parser, and the composer.

World modifiers are not world data and not env-file settings of their own. They are
*launch arguments* to ``valheim_server.x86_64``, which the upstream image passes
through verbatim from ``SERVER_ARGS``. So this module's whole job is turning a set of
structured choices into one string, and that string back into the choices.

Three rules earn this its own module rather than a couple of helpers in ``setup.py``:

**Order is correctness, not style.** Valheim applies these arguments in sequence, so
``-modifier combat hard -preset casual`` ends up *entirely* casual: the preset
overwrites everything before it. ``compose`` therefore always emits ``-preset`` first,
then every ``-modifier``, then every ``-setkey`` -- from its own declaration order, not
from the order a caller happened to fill a dict in, and not from the order a form
renders its fields.

**A default is an omission.** ``normal`` is what every category and the preset fall
back to when the argument is absent, and it is not documented as an argument *value*
for any of them -- ``-modifier combat normal`` risks the server rejecting a flag it
never advertised. So the default is expressed by leaving the category out, and
``normal`` arriving from anywhere (a hand-written file, a posted field) is read as
"default" and dropped.

**The operator's own arguments survive.** Anything in ``SERVER_ARGS`` that is not one
of the flag sequences above is kept token for token and re-emitted after the managed
arguments. This is the ``SERVER_ARGS`` analogue of the settings file's "unknown keys
survive" rule: nobody should lose a flag they added by hand because they touched a
difficulty dropdown. Duplicates among them are kept as duplicates -- merging them
would be a rewrite, which is exactly what is promised not to happen.

Nothing here reads or writes a file, and nothing here decides *whether* a save is
allowed: ``setup.validated_modifiers`` wraps the refusals in the panel's own error
type, and ``main`` composes on the save route.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

PRESET_FLAG = "-preset"
MODIFIER_FLAG = "-modifier"
SETKEY_FLAG = "-setkey"

# The whole documented vocabulary. Nothing outside these is ever written, and a value
# outside them that arrives as a posted field is refused rather than passed through.
# Spelled in the game's own order, `normal` first, because that order is what the
# panel's dropdowns render.
PRESETS: tuple[str, ...] = (
    "normal",
    "casual",
    "easy",
    "hard",
    "hardcore",
    "immersive",
    "hammer",
)

# Insertion order is the order `compose` emits them in; see the module docstring.
CATEGORIES: dict[str, tuple[str, ...]] = {
    "combat": ("veryeasy", "easy", "normal", "hard", "veryhard"),
    "deathpenalty": ("casual", "veryeasy", "easy", "normal", "hard", "hardcore"),
    "resources": ("muchless", "less", "normal", "more", "muchmore", "most"),
    "raids": ("none", "muchless", "less", "normal", "more", "muchmore"),
    "portals": ("casual", "normal", "hard", "veryhard"),
}

TOGGLES: tuple[str, ...] = ("nobuildcost", "playerevents", "passivemobs", "nomap")

# What each control is called in the panel. Here rather than in the template so the
# markup cannot name a category this module does not have.
LABELS: dict[str, str] = {
    "preset": "Preset",
    "combat": "Combat difficulty",
    "deathpenalty": "Death penalty",
    "resources": "Resource rate",
    "raids": "Raid frequency",
    "portals": "Portal rules",
    "nobuildcost": "Building costs nothing",
    "playerevents": "Raids follow players (player events)",
    "passivemobs": "Creatures never attack (passive mobs)",
    "nomap": "No map and no minimap",
}

# The value every category and the preset take when their argument is left out.
DEFAULT_VALUE = "normal"

# The keys a structured submission may carry. Anything else is a category this feature
# does not have, and inventing one is exactly what it must not do.
FIELD_KEYS: tuple[str, ...] = ("preset", *CATEGORIES, "toggles")


class ModifierError(ValueError):
    """A submitted modifier field is outside the documented vocabulary.

    The message names the field and is safe to show the operator. ``setup`` re-raises
    it as ``SetupInputError`` so the panel answers a bad modifier exactly as it answers
    a bad port.
    """


@dataclass(frozen=True)
class Modifiers:
    """One resolved set of choices, plus whatever else ``SERVER_ARGS`` carried.

    ``categories`` holds only the overrides: a category at its default is absent, which
    is the same thing the composed string says by leaving it out.
    """

    preset: str = ""
    categories: Mapping[str, str] = field(default_factory=dict)
    toggles: tuple[str, ...] = ()
    unmanaged: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """The shape the WebUI prefills its controls from."""
        return {
            "preset": self.preset,
            "categories": dict(self.categories),
            "toggles": list(self.toggles),
            # One string rather than a list: the preview appends it verbatim.
            "unmanaged": " ".join(self.unmanaged),
            "server_args": compose(self),
        }


def split_args(value: str) -> list[str]:
    """Split ``SERVER_ARGS`` into tokens, keeping each token exactly as written.

    ``posix=False`` leaves quotes in place, so an operator's ``-name "My Server"``
    survives as two tokens and is re-emitted with its quoting intact. An unbalanced
    quote makes ``shlex`` raise; a whitespace split is then the honest fallback --
    refusing to read the file at all would lock the operator out of the panel over a
    typo in an argument this feature does not even manage.
    """
    text = value or ""
    try:
        return shlex.split(text, posix=False)
    except ValueError:
        return text.split()


def parse(value: str) -> Modifiers:
    """Read ``SERVER_ARGS`` into the choices it encodes plus the rest, verbatim.

    Deliberately lenient: an unknown flag, an unknown category or a value outside the
    vocabulary is not an error here, it is simply not ours -- it lands in ``unmanaged``
    and is preserved. Refusing is for *posted* fields (see ``fields_to_modifiers``),
    because a file the operator hand-edited must still open in the panel.

    Two sequencing facts are modelled because the game behaves that way:

    * a ``-preset`` flattens every ``-modifier`` before it, so those are dropped;
    * a repeated ``-modifier`` for one category is last-wins.
    """
    tokens = split_args(value)
    preset = ""
    categories: dict[str, str] = {}
    toggles: list[str] = []
    unmanaged: list[str] = []
    index = 0
    total = len(tokens)
    while index < total:
        token = tokens[index]
        if token == PRESET_FLAG and index + 1 < total and tokens[index + 1] in PRESETS:
            preset = tokens[index + 1]
            # The preset resets what came before it, so keeping those overrides would
            # show the operator a combat setting the game is about to ignore.
            categories.clear()
            index += 2
            continue
        if (
            token == MODIFIER_FLAG
            and index + 2 < total
            and tokens[index + 1] in CATEGORIES
            and tokens[index + 2] in CATEGORIES[tokens[index + 1]]
        ):
            categories[tokens[index + 1]] = tokens[index + 2]
            index += 3
            continue
        if token == SETKEY_FLAG and index + 1 < total and tokens[index + 1] in TOGGLES:
            if tokens[index + 1] not in toggles:
                toggles.append(tokens[index + 1])
            index += 2
            continue
        unmanaged.append(token)
        index += 1

    # `normal` *is* the default, and the composer never writes it, so it is read as the
    # default here too -- otherwise the panel would show an override that the preview
    # then silently removes.
    if preset == DEFAULT_VALUE:
        preset = ""
    return Modifiers(
        preset=preset,
        categories={
            key: chosen for key, chosen in categories.items() if chosen != DEFAULT_VALUE
        },
        toggles=tuple(toggles),
        unmanaged=tuple(unmanaged),
    )


def compose(modifiers: Modifiers) -> str:
    """Render the choices as ``SERVER_ARGS``, in the order the game requires.

    ``-preset`` first, then the ``-modifier`` pairs in this module's declaration order,
    then the ``-setkey`` flags, then the operator's own arguments. Defaults are absent
    rather than written as ``normal``.
    """
    parts: list[str] = []
    if modifiers.preset and modifiers.preset != DEFAULT_VALUE:
        parts += [PRESET_FLAG, modifiers.preset]
    chosen = modifiers.categories or {}
    for category in CATEGORIES:
        value = chosen.get(category, "")
        if value and value != DEFAULT_VALUE:
            parts += [MODIFIER_FLAG, category, value]
    for toggle in TOGGLES:
        if toggle in modifiers.toggles:
            parts += [SETKEY_FLAG, toggle]
    parts += list(modifiers.unmanaged)
    return " ".join(parts)


def _one_of(raw: Mapping[str, Any], key: str, allowed: Sequence[str]) -> str:
    """A single documented value, or "" for the default. Refuses anything else."""
    if key not in raw:
        return ""
    value = raw[key]
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, str):
        kind = type(value).__name__
        raise ModifierError(
            f"{LABELS.get(key, key)} ({key}) was sent as {kind}. Send one of "
            f"{', '.join(allowed)}, or leave it out for the default."
        )
    chosen = value.strip().lower()
    if not chosen:
        return ""
    if chosen not in allowed:
        raise ModifierError(
            f"{chosen!r} is not a documented value for {LABELS.get(key, key)} "
            f"({key}). Valheim accepts {', '.join(allowed)}."
        )
    # `normal` is the default, and the default is an omission.
    return "" if chosen == DEFAULT_VALUE else chosen


def _toggle_names(raw: Mapping[str, Any]) -> tuple[str, ...]:
    """The ticked toggles, as a list of names or a name -> bool mapping."""
    submitted = raw.get("toggles")
    if submitted is None or submitted == "":
        return ()
    if isinstance(submitted, Mapping):
        names: list[Any] = [name for name, on in submitted.items() if on]
    elif isinstance(submitted, (list, tuple)):
        names = list(submitted)
    else:
        raise ModifierError(
            "toggles must be a list of modifier names "
            f"({', '.join(TOGGLES)}), not {type(submitted).__name__}."
        )
    ticked: list[str] = []
    for name in names:
        if not isinstance(name, str) or name.strip().lower() not in TOGGLES:
            raise ModifierError(
                f"{name!r} is not a Valheim modifier toggle. The documented ones are "
                f"{', '.join(TOGGLES)}."
            )
        cleaned = name.strip().lower()
        if cleaned not in ticked:
            ticked.append(cleaned)
    # Emitted in this module's order, not the order they were ticked in.
    return tuple(toggle for toggle in TOGGLES if toggle in ticked)


def fields_to_modifiers(
    raw: Mapping[str, Any], *, unmanaged: Sequence[str] = ()
) -> Modifiers:
    """Turn a structured submission into ``Modifiers``, refusing anything undocumented.

    ``unmanaged`` is what the current ``SERVER_ARGS`` held outside this vocabulary; it
    is carried through untouched so that composing from these fields cannot drop an
    argument the operator added themselves.
    """
    if not isinstance(raw, Mapping):
        raise ModifierError(
            "The world modifiers must be sent as an object of fields "
            f"({', '.join(FIELD_KEYS)})."
        )
    unknown = sorted(key for key in raw if key not in FIELD_KEYS)
    if unknown:
        # Inventing a category is the one thing this vocabulary must never do, so an
        # unrecognised field is named rather than quietly ignored.
        raise ModifierError(
            f"{', '.join(unknown)} is not a Valheim world modifier. The documented "
            f"categories are {', '.join(CATEGORIES)}, plus preset and toggles."
        )
    return Modifiers(
        preset=_one_of(raw, "preset", PRESETS),
        categories={
            category: chosen
            for category, values in CATEGORIES.items()
            if (chosen := _one_of(raw, category, values))
        },
        toggles=_toggle_names(raw),
        unmanaged=tuple(unmanaged),
    )


def canonical(value: str) -> str:
    """``value`` unchanged, or a refusal because this feature would not have written it.

    The one check on a ``SERVER_ARGS`` *string* rather than on fields: it must already
    be exactly what ``compose`` produces -- preset first, defaults left out, the
    operator's own arguments last. That is what lets the panel promise the preview is
    the string that lands in the file, and it is why modifiers are posted as fields and
    composed here instead of arriving pre-built from a browser.
    """
    text = (value or "").strip()
    rendered = compose(parse(text))
    if rendered != text:
        raise ModifierError(
            "SERVER_ARGS is composed by the manager from the modifier controls: "
            "-preset first, then each -modifier, then each -setkey, with defaults left "
            "out and your own arguments kept after them. Send the modifier fields "
            f"rather than a ready-made string (this one would be written as "
            f"{rendered!r})."
        )
    return rendered


__all__ = [
    "CATEGORIES",
    "DEFAULT_VALUE",
    "FIELD_KEYS",
    "LABELS",
    "MODIFIER_FLAG",
    "PRESETS",
    "PRESET_FLAG",
    "SETKEY_FLAG",
    "TOGGLES",
    "ModifierError",
    "Modifiers",
    "canonical",
    "compose",
    "fields_to_modifiers",
    "parse",
    "split_args",
]
