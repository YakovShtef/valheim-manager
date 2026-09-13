---
title: 'World Modifiers in the Settings Panel'
type: 'feature'
created: '2026-09-13'
status: 'in-progress'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: 'NO_VCS'
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Valheim's world modifiers — combat difficulty, death penalty, resource rate, raid frequency, portal rules, and toggles like no-map — can only be set as launch arguments, which this stack has no home for at all. `SERVER_ARGS` appears in neither the settings file nor the UI, so the only way to change difficulty is to hand-write launch flags the manager does not know about.

**Approach:** Add a modifiers section to the settings panel that reads and writes `SERVER_ARGS`, using the documented vocabulary, composing the arguments in the order Valheim requires, and showing the exact resulting string before it is saved. Applying uses the panel's existing rule: the server must be off, and saving recreates the container on the next Start.

## Boundaries & Constraints

**Always:**
- Only Valheim's documented vocabulary is accepted: preset `normal|casual|easy|hard|hardcore|immersive|hammer`; `combat veryeasy|easy|normal|hard|veryhard`; `deathpenalty casual|veryeasy|easy|normal|hard|hardcore`; `resources muchless|less|normal|more|muchmore|most`; `raids none|muchless|less|normal|more|muchmore`; `portals casual|normal|hard|veryhard`; toggles `nobuildcost|playerevents|passivemobs|nomap`.
- Composed in the order the game requires: `-preset` first, then every `-modifier <category> <value>`, then every `-setkey <name>`. A preset emitted after a modifier silently flattens it back, so order is correctness, not style.
- A category left at its default is omitted entirely rather than written as `normal` — leaving it out is what keeps the default, and `normal` is not a documented argument value.
- Arguments already in `SERVER_ARGS` that this feature does not manage are preserved verbatim and kept after the managed ones. Editing difficulty must never drop an operator's own flags.
- The composed string is shown in the UI exactly as it will be written, before saving.
- Everything inherits the settings panel's existing rules: editing only while the server is off, re-checked server-side on save; the same origin and session guards; the atomic comment-preserving write; and removing the stopped container when a value changed.

**Never:**
- No modifiers in the first-run wizard — this is a post-install panel feature only.
- Never invent or pass through a category, value or toggle outside the list above; an unknown one is refused, not written.
- Never reorder or rewrite the operator's unmanaged arguments, and never merge duplicates of them.
- No world upload/delete/seed, no backups, no mod toggles, no admin-list editing.
- Modifiers are launch arguments, not world data: never imply that changing them alters or migrates an existing world.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| No modifiers yet | `SERVER_ARGS` absent or empty | Every category shows its default, no toggles ticked, preview empty | — |
| Existing modifiers | `SERVER_ARGS=-preset hard -modifier raids more -setkey nomap` | Controls reflect exactly that; preview reproduces it | An unparseable or unknown token is shown as unmanaged and left alone, not silently dropped |
| Compose a selection | Preset plus two category overrides and two toggles | `SERVER_ARGS` written as preset → modifiers → setkeys, defaults omitted | — |
| Reset a category | A category set back to its default | Its `-modifier` token is removed from `SERVER_ARGS`; the others are untouched | — |
| Operator's own flags | `SERVER_ARGS` carries an argument this feature does not manage | Preserved verbatim, placed after the managed arguments | — |
| Unknown value posted | A category, value or toggle outside the vocabulary | Refused naming the field; nothing written and no container removed | Same refusal shape as the panel's other validation errors |
| Server running | Running, restarting, paused, or a start/stop in flight | Modifier controls locked with the panel's existing reason | Save posted anyway is refused without writing |

</frozen-after-approval>

## Code Map

- `manager/app/settings_store.py` -- `write(updates)` already preserves comments, ordering and unknown keys, so `SERVER_ARGS` is just another key. `DEFAULT_SETTINGS_TEXT` has no `SERVER_ARGS` line yet and needs one (commented, empty) so the file documents it; `test_default_settings_text_matches_the_shipped_example` pins it against `valheim.env.example`, which needs the same line.
- `manager/app/setup.py` -- `validated_settings(raw, *, current=None)` and `SETTINGS_KEYS` are the shared validation the panel already uses; modifiers must plug into the same path so the panel cannot write a value the validator would reject. `one_line` strips CR/LF.
- `manager/app/main.py` -- `POST /api/settings` already proves the server is off, diffs, validates, writes and removes the stopped container; `_submitted_settings` maps the request body to editable keys, and `_settings_payload` is the refresh shape. Modifiers should arrive as structured fields and be composed server-side, not posted as a pre-built string.
- `manager/app/templates/index.html` + `manager/app/static/app.js` -- the editor form, its Edit/Save/Cancel flow, the lock reason and the refresh-without-clobbering-typing behaviour are all in place; the modifier controls join that form.
- Upstream image: `SERVER_ARGS` is its documented passthrough for extra server CLI arguments, so no image change is needed.
- Valheim's own behaviour (verified): leaving a modifier out keeps its default, and a `-preset` after a `-modifier` resets it — the two facts the ordering and omission rules come from.

## Tasks & Acceptance

**Execution:**
- [x] `manager/app/modifiers.py` -- new: the vocabulary, a parser that splits an existing `SERVER_ARGS` into managed selections plus unmanaged leftovers, and a composer that renders preset → modifiers → setkeys and appends the leftovers
- [x] `manager/app/setup.py` -- accept `SERVER_ARGS` as an editable key and validate a composed value against the vocabulary, reusing the existing refusal shape
- [x] `manager/app/settings_store.py` + `valheim.env.example` -- add the documented, empty `SERVER_ARGS` line to both so they stay in step
- [x] `manager/app/main.py` -- take structured modifier fields on the save route, compose them server-side, and include the result in the same write and container-removal path as the other settings
- [x] `manager/app/templates/index.html` + `manager/app/static/app.js` -- the modifier controls (preset, five categories, four toggles) and a live preview of the exact string to be written
- [x] `README.md` -- document the modifiers, that they apply on the next Start, and that they change rules rather than the world itself
- [x] `manager/app/tests/test_edge_cases.py` -- cover the matrix: parse/compose round-trip, ordering, default omission, unmanaged arguments preserved, unknown values refused, and the locked-while-running case

**Acceptance Criteria:**
- Given a preset, two category overrides and two toggles, when the operator saves, then `SERVER_ARGS` contains them in preset → modifier → setkey order with defaulted categories absent.
- Given a `SERVER_ARGS` holding an argument this feature does not manage, when the operator changes a category and saves, then that argument survives verbatim.
- Given an existing `SERVER_ARGS`, when the editor is opened, then the controls show exactly the values it encodes and the preview reproduces the string.
- Given a category is set back to its default, when the operator saves, then its `-modifier` token is gone and the others are unchanged.
- Given a value outside the documented vocabulary, when a save is posted, then it is refused naming the field, nothing is written and no container is removed.
- Given the server is running, when the operator views the panel, then the modifier controls are locked with the same reason as the rest of the panel.

## Implementation Notes

Four decisions the spec left open, and why they were settled this way:

- **`normal` is the default, so it is never written.** The vocabulary lists `normal` for
  the preset and for all five categories, while the omission rule says a category at its
  default is left out "rather than written as `normal`". Those only reconcile if
  `normal` *is* the default, so it is accepted wherever a value is accepted and then
  read as "left at default": the dropdowns offer it only as the default option, and a
  posted `normal` composes to nothing. Nothing outside the documented lists is ever
  accepted.
- **Refusal is for posted fields; leniency is for the file.** A category, value or
  toggle outside the vocabulary arriving as a *field* is refused naming the field
  (`_one_of` / `_toggle_names`). The same thing found while *reading* an existing
  `SERVER_ARGS` is treated as unmanaged and preserved, because a file the operator
  hand-edited still has to open in the panel. The one string-level check,
  `modifiers.canonical`, refuses a `SERVER_ARGS` that is not what the composer would
  have produced, which is what keeps "the preview is the string that gets written"
  true; a ready-made `SERVER_ARGS` inside `settings` is ignored like any other key the
  panel does not own, so modifiers can only arrive as fields.
- **A `-preset` after a `-modifier` is read as flattening it.** The spec calls the
  ordering a correctness rule, so the parser models the game's behaviour rather than
  showing the operator an override the server is about to discard. Such a value
  re-composes preset-first with the flattened categories dropped — semantically
  identical, and shown in the preview before anything is saved.
- **`SERVER_ARGS=` ships uncommented and empty** in both `DEFAULT_SETTINGS_TEXT` and
  `valheim.env.example`. A commented-out line would document the key just as well but
  would make the first save append a bare `SERVER_ARGS=...` under an "Added by the
  Valheim manager" heading, away from the comment that explains it; with the key
  present, the writer replaces it in place. Both files still parse to the same mapping,
  which is what `test_default_settings_text_matches_the_shipped_example` pins.

The panel's existing rules carried over untouched: the modifier controls live inside the
same editor form, so the server-off lock, the reason text, the origin and session
guards, the atomic comment-preserving write and the stopped-container removal all apply
to `SERVER_ARGS` without a second mechanism.

## Spec Change Log

## Review Triage Log

## Design Notes

Ordering is the subtle part. Valheim applies these arguments in sequence, so `-modifier combat hard -preset casual` ends up entirely casual — the preset overwrites what came before it. Composing preset-first is therefore a correctness rule, and a test should pin it rather than trusting the renderer's field order.

Omitting defaults matters for the same reason: `normal` is not a documented value for any category, so writing `-modifier combat normal` risks the server rejecting an argument it never advertised. Leaving the category out is the documented way to get the default.

Preserving unmanaged arguments is the `SERVER_ARGS` analogue of the settings file's "unknown keys survive" rule — an operator who added their own flag should not lose it by touching a difficulty dropdown.

## Verification

**Commands:**
- `cd manager && python -m pytest` -- expected: passes, including the new modifier cases
- `docker compose config -q` -- expected: still valid with no env files present

**Manual checks (if no CLI):**
- Set a preset plus overrides, confirm the preview matches what lands in `settings/valheim.env`
- Add a custom flag to `SERVER_ARGS` by hand, change a dropdown, and confirm the flag survives
- Start the server and confirm the modifier controls lock with the same reason as the rest of the panel
