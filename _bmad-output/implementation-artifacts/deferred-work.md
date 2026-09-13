- source_spec: `_bmad-output/implementation-artifacts/spec-valheim-docker-manager.md`
  summary: In-UI editor for core Valheim server settings (name, password, world, port, public, crossplay) that applies changes by recreating the container from a clone of its existing config.
  evidence: Split at the step-02 token gate — the combined spec reached ~2,700 tokens against a 1,600 ceiling. Independently shippable: until it lands, settings come from the env file the compose stack already reads, and changing one means Stop, `docker rm`, Start. DELIVERED by `spec-valheim-editable-settings-panel.md`: the panel edits those six fields while the server is off and removes the stopped container itself, so the manual Stop / `docker rm` / Start sequence is now the advanced, hand-edit path only.

- source_spec: `_bmad-output/implementation-artifacts/spec-valheim-docker-manager.md`
  summary: Manual "Backup now" control that triggers the image's internal backup service and confirms success only when a new archive appears under the backups volume.
  evidence: Split at the step-02 token gate — the combined spec reached ~2,700 tokens against a 1,600 ceiling. Independently shippable: the image's own hourly backup cron keeps running without it.

- source_spec: `_bmad-output/implementation-artifacts/spec-valheim-docker-manager.md`
  summary: Mod management — BepInEx and ValheimPlus install/toggle from the WebUI.
  evidence: Scoped out by the human at step-02 as "Core only" before the first build, to keep v1 to one cohesive goal. The upstream image supports mods via its own env vars in the meantime.

- source_spec: `_bmad-output/implementation-artifacts/spec-valheim-docker-manager.md`
  summary: Admin / ban / permit-list editing from the WebUI (the `adminlist.txt`, `bannedlist.txt`, `permittedlist.txt` files on the `/config` volume).
  evidence: Scoped out by the human at step-02 as "Core only". Also excluded by the spec's Never list, which rules out in-game admin commands such as kick and ban. Editable directly on the volume for now.

- source_spec: `_bmad-output/implementation-artifacts/spec-valheim-docker-manager.md`
  summary: Backup browse and restore UI — list the archives under `/config/backups` and restore a chosen one.
  evidence: Scoped out by the human at step-02 as "Core only". Restore is a destructive, world-overwriting operation that needs its own confirmation design; deferring it keeps v1's blast radius to start/stop/logs.

- source_spec: `_bmad-output/implementation-artifacts/spec-valheim-ui-config-and-setup.md`
  summary: Post-install settings panel with editable core fields plus structured Valheim world modifiers (preset, combat/deathpenalty/resources/raids/portals, and the nobuildcost/playerevents/passivemobs/nomap toggles) composed into SERVER_ARGS, applied by recreating the game container from a clone of its existing config.
  evidence: Split at the step-02 token gate — the combined install-plus-editing spec reached ~2,400 tokens against the 1,600 ceiling. Install went first because hand-generating a bcrypt hash currently blocks installation outright, and the wizard's atomic settings writer is the foundation this panel builds on. Supersedes the earlier settings-editor entry above by adding the modifier vocabulary (verified) and the recreate-from-clone requirement. Until it lands, settings are whatever the wizard wrote, changed by editing the file and doing Stop → `docker rm` → Start. PARTLY DELIVERED by `spec-valheim-editable-settings-panel.md`: the core fields (name, world, port, join password, public, crossplay) are now editable in the panel while the server is off, and a save removes the stopped container. DELIVERED in full by `spec-valheim-world-modifiers.md`: the structured world-modifier vocabulary is composed into `SERVER_ARGS` by `manager/app/modifiers.py` and saved through the same panel path. Note the one deliberate departure from this entry's wording — applying still recreates the container from the settings file (Save removes the stopped container, the next Start builds a new one), not from a clone of the existing container's config; the panel's established rule was kept rather than introduced a second apply mechanism for one key.

- source_spec: `_bmad-output/implementation-artifacts/spec-valheim-ui-config-and-setup.md`
  summary: Add an explicit Origin check to the `/ws/logs` WebSocket handshake, reusing the allowlist `require_same_origin` already builds for the POST routes.
  evidence: Raised by the blind-hunter layer and confirmed real: the socket validates the session cookie but never inspects Origin, unlike `/login`, `/logout`, `/setup` and `/api/start|stop|restart`. Deferred rather than patched because it predates this build (the WebSocket is unchanged here) and `SameSite=Strict` is the actual barrier today — browsers apply it to WebSocket handshakes. Worth doing as defence in depth, with a cross-origin handshake test.

- source_spec: `_bmad-output/implementation-artifacts/spec-valheim-ui-config-and-setup.md`
  summary: Smoke-test that the socket proxy's endpoint allowlist still covers every Docker call the manager makes (pull, create, start, stop, kill, remove, logs).
  evidence: Filed by the verification-gap layer with disposition `defer`, and verified as a genuine hole: setting `ALLOW_STOP: 0` or `ALLOW_RESTARTS: 0` leaves the entire suite green while Stop and Force stop break in production, because every test runs against the fake engine. Closing it needs a live Docker daemon plus the proxy container, which this suite deliberately avoids — it belongs in a manual or CI smoke step, not in the unit suite.
