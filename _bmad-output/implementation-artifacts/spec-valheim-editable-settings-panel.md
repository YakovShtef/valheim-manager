---
title: 'Editable Current Settings Panel, Gated on the Server Being Off'
type: 'feature'
created: '2026-09-13'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: 'NO_VCS'
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The "Current settings" panel is read-only. Changing the server name, password, world or port means hand-editing `settings/valheim.env` and then Stop → `docker rm` → Start, because an existing container keeps the environment it was created with. That is undiscoverable from the UI and easy to half-do, leaving the server quietly running old values.

**Approach:** Give the panel an Edit button, usable whenever the server is off. Saving validates exactly as the setup wizard does, writes through the existing atomic writer, and removes the stopped container so the next Start recreates it with the new settings.

## Boundaries & Constraints

**Always:**
- Editing is allowed only while the server is off: no container, or one that is not running, restarting or paused. While it runs the panel stays read-only and says to stop the server first.
- The server-off condition is re-checked on the server when the save arrives, not only in the browser.
- Saving goes through `SettingsStore.write`, preserving comments, blank lines, key order and unrecognised keys.
- A save that changes at least one value also removes the stopped container, so the next Start recreates it. The UI must say so, and say the world and backups live in volumes and are untouched. A save that changes nothing removes nothing.
- Validation is shared with the wizard, not reimplemented — including the join password's 5-character minimum.
- Secrets stay masked, and submitting without retyping a masked field leaves the stored value intact. The mask is never written as a value.
- Saving requires an authenticated session and the same origin check as the other state-changing routes.

**Never:**
- No editing, queuing or applying while the server runs; no apply-and-restart.
- No world modifiers here — `SERVER_ARGS` has no home in the settings file yet and stays deferred.
- No world upload/delete/seed, no backups, no mod toggles, no admin-list editing.
- Never render the real join password into the page, and never log it.
- Never remove a running container, and never touch the `valheim-config` or `valheim-data` volumes.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Open editor, server off | No container, or a stopped one | Fields become editable, prefilled, secrets masked rather than shown | Unreadable/non-UTF-8 file: existing named error, editor does not open |
| Server running | Running, restarting or paused | Panel stays read-only, naming the reason | Edit control disabled, not hidden |
| Save valid changes | Server off, stopped container exists, values changed | File updated with comments and unknown keys intact; stopped container removed; panel shows new values | Removal failure: say so with the Docker error — the file is written, so the operator is told Start still needs it gone |
| Save with no changes | Nothing edited | No-op: nothing written, no container removed | — |
| Untouched masked secret | Only the server name edited | Stored join password preserved exactly | — |
| Invalid value | Password under 5, port outside 1-65533, world name with a slash, empty server name | Offending field named; nothing written, no container removed | Same messages the wizard gives |
| Started between load and save | Server started elsewhere after the editor opened | Save refused with the running reason; nothing written | Panel returns to read-only |

</frozen-after-approval>

## Code Map

- `manager/app/templates/index.html` -- the read-only panel and its "Not editable here yet" paragraph (which this replaces), plus the `#settings-table` the JS rebuilds.
- `manager/app/static/app.js` -- `renderSettings` rebuilds that table on every status push, skipping the DOM when its `data-signature` matches; an open editor must survive that refresh. `post()` already handles errors, disabling and the banner.
- `manager/app/setup.py` -- `_validated_settings` is exactly the validation needed (server name, world name slash rule, port range, join password minimum, `SERVER_PUBLIC`/`CROSSPLAY` values) plus `_one_line`'s CR/LF stripping; `INITIAL_SETTINGS_KEYS` is the same key set. Extract and share.
- `manager/app/settings_store.py` -- `write(updates)` is atomic and comment-preserving; `display_settings()` masks via `SECRET_KEY_PATTERN` and flags which rows are secret; `read()` gives real values for the diff.
- `manager/app/docker_control.py` -- `status()` reports `container_exists`/`container_state`/`phase`; `_remove_container` is private and used only for failed-create cleanup, so the apply path needs a public removal that refuses unless the container is off.
- `manager/app/main.py` -- `require_session` + `require_same_origin` guard state-changing routes; `display_settings()` and `safe_status()` already feed the panel; `_run_action`'s error shape models the save route's failures.

## Tasks & Acceptance

**Execution:**
- [x] `manager/app/setup.py` -- extract the field validation and its key set so wizard and panel share one implementation, with no behaviour change to the wizard
- [x] `manager/app/settings_store.py` -- expose what the editor needs to tell "secret left untouched" from a submitted value, without rendering the real secret
- [x] `manager/app/docker_control.py` -- public remove-the-stopped-container operation that refuses while running, restarting or paused
- [x] `manager/app/main.py` -- the save route behind auth + origin: re-check the server is off, validate, write, remove the stopped container when something changed, return refreshed settings and status
- [x] `manager/app/templates/index.html` -- Edit control and form, the "world and backups are untouched" wording, and the reason text when the server runs
- [x] `manager/app/static/app.js` -- Edit/Save/Cancel, editing disabled whenever the status is not off, and the periodic refresh must not discard what the operator is typing
- [x] `README.md` -- replace the manual Stop → `docker rm` → Start procedure with the panel, keeping the hand-edit path as the advanced option
- [x] `manager/app/tests/test_edge_cases.py` -- cover the matrix: refused while running (including the start-after-load race), comments and unknown keys preserved, masked secret preserved when untouched, invalid values writing nothing, the no-change no-op, and removal happening exactly when a value changed

**Acceptance Criteria:**
- Given the server is off and a stopped container exists, when the operator edits a value and saves, then the file keeps its comments and unknown keys, the stopped container is gone, and the panel shows the new values.
- Given the server is running, when the operator looks at the panel, then editing is disabled with the reason, and a save posted anyway is refused without writing.
- Given only the server name is changed, when the operator saves, then the stored join password is unchanged and the mask was never written.
- Given an invalid value, when the operator saves, then the field is named, nothing is written, and no container is removed.
- Given nothing was changed, when the operator saves, then nothing is written and the stopped container stays.

## Implementation Notes

**Shared validation lives in `setup.py`.** `_validated_settings` became
`validated_settings(raw, *, current=None)` and `_one_line` became `one_line`;
`INITIAL_SETTINGS_KEYS` became `SETTINGS_KEYS`. The wizard's call is unchanged in
behaviour (it passes every key and no `current`), which the existing wizard tests
still prove.

**Only what changed is validated.** The panel submits six fields but the route
diffs them against the file first and validates just the difference. Validating the
whole submission would let a value that is *already* wrong in the file block an
unrelated edit -- an empty `SERVER_PASS` (which `_refuse_doomed_start` already
refuses at Start) would otherwise make the server name unfixable from the panel.
`current` exists for the one rule that needs the other half of the pair: renaming a
server so the stored join password appears inside the name is still refused.

**The mask is dropped before validation, not after.** `is_untouched_secret` sits in
`settings_store` next to `MASK` and `display_settings`, so the thing that renders the
mask and the thing that recognises it cannot drift. `********` is eight characters
and would pass the length rule, so dropping it late would have written it.

**Order is the contract:** prove the server is off, validate, render-check every
value (`format_env_value`), write, then remove. Everything that can refuse runs
before the write, so a refusal leaves the file byte-identical.

**Response shapes.** Every refusal goes through one helper and answers the same
shape -- the reason plus a refreshed `status` + `settings` + `saved: false` -- so a
save that is both invalid *and* racing a start re-locks the panel on the spot instead
of leaving the editor open over it. 400 is for what the operator sent (a bad value, a
field sent as `null` or an object); 500 is for the file itself, whether the failure is
the read or the write; 409 is the server-on refusal; 502 is Docker. The removal
failure is the one 502 with `saved: true`: the file *is* written, and the operator is
told the container still holds the old environment and named `docker rm`.

**`running_reason()` also covers the manager's own in-flight phases** (pulling,
creating, starting, stopping), not just the container state. The spec's condition is
about container state; refusing during a start already underway is strictly more
conservative and keeps the browser's rule and the server's rule identical. It returns
the advice with the reason, because the two differ: mid-pull there is nothing running
to stop yet, and mid-stop the operator has already asked for it and only has to wait.

**Browser-verified, and it caught one bug the suite could not:** `.settings-form`
sets `display: grid`, a class selector, which beats the UA stylesheet's
`[hidden] { display: none }` -- the closed editor sat open under the table. Fixed
with `.settings-form[hidden] { display: none; }` (and the same guard on the table).
Verified in a real browser against the suite's fake engine: open/prefill (secret
masked), refuse-and-keep-typing on an invalid value, successful save closing the
editor, the container disappearing, a start from "another tab" closing an open editor
mid-edit while keeping what was typed for the next Edit, an untouched mask never
reaching the wire, and Edit staying disabled when the file cannot be read at all.

## Spec Change Log

## Review Triage Log

## Design Notes

Removing the stopped container is the feature, not a side effect: `start()` creates a container only when none exists, so without removal a save would write the file and the next Start would still run the old environment — the exact trap the current panel warns about. The world, backups and server install live in the `valheim-config` and `valheim-data` volumes, so removal costs nothing, which is why the UI can say so plainly.

The masked-secret rule is the subtle one. `display_settings()` renders `SERVER_PASS` as `********`; a form posting its fields back verbatim would store that literal string as the join password — locking everyone out of the game with a value that still looks plausible in the panel.

## Verification

**Commands:**
- `cd manager && python -m pytest` -- expected: passes, including the new panel cases
- `docker compose config -q` -- expected: still valid with no env files present

**Manual checks (if no CLI):**
- With the server off, edit each field and confirm the file keeps its comments and key order
- Change only the server name, then confirm the join password still works
- Start the server, confirm the panel is read-only with the reason, and that a hand-posted save is refused
