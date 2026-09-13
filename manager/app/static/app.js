// Valheim manager WebUI: one WebSocket carries both status pushes and log lines.
// No build step, no dependencies.
(function () {
  "use strict";

  var MAX_CONSOLE_LINES = 4000;
  var RECONNECT_MIN_MS = 1000;
  var RECONNECT_MAX_MS = 15000;

  // What the panel shows instead of a secret. Read off the form rather than written
  // out here, so there is one definition (settings_store.MASK) and no copy to drift.
  var MASK = "********";
  // The only phases in which settings may be edited: no container, or one that is down.
  var OFF_PHASES = { absent: 1, stopped: 1 };

  var el = {
    badge: document.getElementById("status-badge"),
    link: document.getElementById("link-badge"),
    message: document.getElementById("status-message"),
    state: document.getElementById("meta-state"),
    started: document.getElementById("meta-started"),
    image: document.getElementById("meta-image"),
    banner: document.getElementById("banner"),
    console: document.getElementById("console"),
    follow: document.getElementById("follow"),
    settings: document.querySelector("#settings-table tbody"),
    settingsTable: document.getElementById("settings-table"),
    settingsForm: document.getElementById("settings-form"),
    settingsEdit: document.getElementById("btn-settings-edit"),
    settingsSave: document.getElementById("btn-settings-save"),
    settingsCancel: document.getElementById("btn-settings-cancel"),
    settingsLocked: document.getElementById("settings-locked"),
    modifierFields: document.getElementById("modifier-fields"),
    modifierPreview: document.getElementById("modifier-preview"),
    modifierUnmanaged: document.getElementById("modifier-unmanaged-note"),
    start: document.getElementById("btn-start"),
    stop: document.getElementById("btn-stop"),
    restart: document.getElementById("btn-restart"),
    force: document.getElementById("btn-force"),
    clear: document.getElementById("btn-clear")
  };

  var PHASES = {
    absent:   { label: "no container", cls: "badge-stopped" },
    stopped:  { label: "stopped",      cls: "badge-stopped" },
    pulling:  { label: "pulling image", cls: "badge-busy" },
    creating: { label: "creating",     cls: "badge-busy" },
    starting: { label: "starting",     cls: "badge-busy" },
    stopping: { label: "stopping",     cls: "badge-busy" },
    running:  { label: "running (not yet ready)", cls: "badge-running" },
    ready:    { label: "ready",        cls: "badge-ready" },
    paused:   { label: "paused",       cls: "badge-stopped" },
    error:    { label: "error",        cls: "badge-error" }
  };

  var busyPhases = { pulling: 1, creating: 1, starting: 1, stopping: 1 };
  var socket = null;
  var backoff = RECONNECT_MIN_MS;
  var everConnected = false;
  var pendingAction = false;
  var lastStatus = null;
  // The rows of the last status push, which is what the editor opens on.
  var lastRows = null;
  // The world modifiers SERVER_ARGS encodes, parsed by the manager and pushed with the
  // status. Parsing them here would mean a second copy of Valheim's vocabulary.
  var lastModifiers = null;
  var editing = false;
  // What was typed into an editor that a start closed under the operator, held until
  // the next Edit so nothing they wrote is lost to someone else's button press.
  var draft = null;
  // A stop timeout leaves the container running, so the very next status push says
  // "running" -- which must NOT retract the force-stop option the operator now
  // needs. Cleared only once the container is actually down, or on a force attempt.
  var forceOffered = false;

  // ---------------------------------------------------------------- console

  function atBottom() {
    return el.console.scrollHeight - el.console.scrollTop - el.console.clientHeight < 40;
  }

  function append(text, cls) {
    var stick = el.follow.checked || atBottom();
    var node = document.createElement("span");
    if (cls) { node.className = cls; }
    node.textContent = text + "\n";
    el.console.appendChild(node);
    while (el.console.childNodes.length > MAX_CONSOLE_LINES) {
      el.console.removeChild(el.console.firstChild);
    }
    if (stick) { el.console.scrollTop = el.console.scrollHeight; }
  }

  function system(text) { append("── " + text + " ──", "sys"); }

  // ----------------------------------------------------------------- banner

  // Which condition painted the banner. Status and settings errors clear themselves
  // when they recover; a failed button press does not, because nothing else would
  // tell the operator their click failed -- it is cleared by the next click instead.
  var bannerSource = null;

  function showError(text, source) {
    el.banner.textContent = text;
    el.banner.className = "banner banner-error";
    el.banner.hidden = false;
    bannerSource = source || "action";
  }

  function clearError() {
    el.banner.hidden = true;
    el.banner.textContent = "";
    bannerSource = null;
  }

  // Recovery: a transient log/settings/status error must not leave a red banner
  // sitting beside a green "ready" badge for an operator who never presses a button.
  function clearErrorFrom(source) {
    if (bannerSource === source) { clearError(); }
  }

  // ----------------------------------------------------------------- status

  function renderStatus(status) {
    lastStatus = status;
    var phase = PHASES[status.phase] || PHASES.error;
    el.badge.textContent = phase.label;
    el.badge.className = "badge " + phase.cls;
    el.message.textContent = status.message || "";
    el.state.textContent = status.container_state || (status.container_exists ? "?" : "no container");
    el.started.textContent = status.started_at && status.started_at.indexOf("0001-01-01") !== 0
      ? new Date(status.started_at).toLocaleString()
      : "–";
    if (status.image) { el.image.textContent = status.image; }

    var busy = pendingAction || !!busyPhases[status.phase];
    var running = status.phase === "running" || status.phase === "ready" || status.phase === "paused";
    el.start.disabled = busy || running;
    el.stop.disabled = busy || !running;
    el.restart.disabled = busy || !status.container_exists;
    if (status.phase === "stopped" || status.phase === "absent") { forceOffered = false; }
    if (forceOffered) {
      el.force.hidden = false;
      el.force.disabled = pendingAction;
    } else if (!busy && status.phase !== "stopping") {
      el.force.hidden = true;
    }

    if (status.error) {
      showError(status.error + (status.docker_error ? " — " + status.docker_error : ""), "status");
    } else {
      clearErrorFrom("status");
    }

    syncSettingsControls();
  }

  // -------------------------------------------------------- settings editor

  // Why editing is refused, or null when the server is off. The manager re-checks this
  // for itself when a save arrives -- this is only about not offering what would be
  // refused.
  function settingsLockReason(status) {
    if (OFF_PHASES[status.phase]) { return null; }
    if (status.phase === "error") {
      return "The manager cannot reach Docker, so it cannot tell whether the server is" +
        " running. Settings stay read-only until it can.";
    }
    var label = (PHASES[status.phase] || PHASES.error).label;
    return "The server is " + label + ". Stop it before changing settings: an existing" +
      " container keeps the environment it was created with, so a new value could not" +
      " take effect anyway.";
  }

  function syncSettingsControls() {
    if (!el.settingsEdit || !el.settingsForm) { return; }
    var reason = lastStatus ? settingsLockReason(lastStatus) : null;
    if (editing && reason) {
      // Started from another tab or from the host while the editor was open. Back to
      // read-only, rather than leaving a form open over a save the manager would refuse
      // -- but keep what was typed, so re-pressing Edit does not cost the operator a
      // half-written server name and a retyped join password.
      draft = { settings: collectSettings(), modifiers: collectModifiers() };
      editing = false;
      system("settings editor closed, your changes are kept — " + reason);
    }
    // Without rows there is nothing to prefill from, and an editor opened on a settings
    // error would offer to save six empty fields.
    var haveValues = !!(lastRows && lastRows.length);
    el.settingsEdit.disabled =
      !lastStatus || !haveValues || !!reason || pendingAction || editing;
    el.settingsLocked.textContent = reason || "";
    el.settingsLocked.hidden = !reason;
    el.settingsSave.disabled = pendingAction;
    el.settingsCancel.disabled = pendingAction;
    el.settingsForm.hidden = !editing;
    // One copy of the values on screen, not two: the table is the read-only view.
    el.settingsTable.hidden = editing;
  }

  function isOn(value) {
    return value === "1" || value === "true" || value === "yes" || value === "on";
  }

  // ------------------------------------------------------ world modifiers
  //
  // The vocabulary and, crucially, the ORDER live in app/modifiers.py and reach this
  // page as server-rendered controls. Walking them in document order is what lets the
  // preview promise it is the string the manager will write: no list of categories is
  // repeated here, so the two cannot drift.

  function modifierSelects() {
    return el.modifierFields
      ? el.modifierFields.querySelectorAll("[data-modifier-field]") : [];
  }

  function modifierToggles() {
    return el.modifierFields
      ? el.modifierFields.querySelectorAll("[data-modifier-toggle]") : [];
  }

  function unmanagedArgs() {
    // Never edited here, always the file's: whatever SERVER_ARGS holds that is not a
    // Valheim world modifier is the operator's own, and it is carried through.
    return (lastModifiers && lastModifiers.unmanaged) || "";
  }

  function composedModifiers() {
    var parts = [];
    var selects = modifierSelects();
    var i;
    var name;
    for (i = 0; i < selects.length; i++) {
      name = selects[i].getAttribute("data-modifier-field");
      if (!selects[i].value) { continue; }  // default: the argument is left out
      parts.push(name === "preset"
        ? "-preset " + selects[i].value
        : "-modifier " + name + " " + selects[i].value);
    }
    var boxes = modifierToggles();
    for (i = 0; i < boxes.length; i++) {
      if (boxes[i].checked) {
        parts.push("-setkey " + boxes[i].getAttribute("data-modifier-toggle"));
      }
    }
    if (unmanagedArgs()) { parts.push(unmanagedArgs()); }
    return parts.join(" ");
  }

  function renderModifierPreview() {
    if (!el.modifierPreview) { return; }
    var composed = composedModifiers();
    el.modifierPreview.textContent =
      composed || "(empty — every modifier is at Valheim's default)";
    el.modifierPreview.className = composed ? "snippet" : "snippet is-empty";
    if (el.modifierUnmanaged) {
      el.modifierUnmanaged.textContent = unmanagedArgs()
        ? "Kept from the current SERVER_ARGS and left after the modifiers, because " +
          "these are not modifiers the panel manages: " + unmanagedArgs()
        : "";
      el.modifierUnmanaged.hidden = !unmanagedArgs();
    }
  }

  function fillModifiers(mods) {
    if (!el.modifierFields) { return; }
    var source = mods || {};
    var categories = source.categories || {};
    var ticked = source.toggles || [];
    var selects = modifierSelects();
    var i;
    var wanted;
    for (i = 0; i < selects.length; i++) {
      wanted = selects[i].getAttribute("data-modifier-field") === "preset"
        ? (source.preset || "")
        : (categories[selects[i].getAttribute("data-modifier-field")] || "");
      selects[i].value = wanted;
      // A value this page has no option for must not silently become a different
      // setting; show the default instead. The manager keeps the file's value until
      // something is actually saved.
      if (selects[i].value !== wanted) { selects[i].value = ""; }
    }
    var boxes = modifierToggles();
    for (i = 0; i < boxes.length; i++) {
      boxes[i].checked =
        ticked.indexOf(boxes[i].getAttribute("data-modifier-toggle")) !== -1;
    }
    renderModifierPreview();
  }

  function collectModifiers() {
    var body = { toggles: [] };
    var selects = modifierSelects();
    var i;
    for (i = 0; i < selects.length; i++) {
      body[selects[i].getAttribute("data-modifier-field")] = selects[i].value;
    }
    var boxes = modifierToggles();
    for (i = 0; i < boxes.length; i++) {
      if (boxes[i].checked) {
        body.toggles.push(boxes[i].getAttribute("data-modifier-toggle"));
      }
    }
    return body;
  }

  function openEditor() {
    // A draft only exists when a start closed the editor under the operator; otherwise
    // the file as the manager last reported it is the truth to edit.
    var values = draft ? draft.settings : null;
    var mods = draft ? draft.modifiers : lastModifiers;
    var i;
    if (!values) {
      values = {};
      for (i = 0; lastRows && i < lastRows.length; i++) {
        values[lastRows[i].key] = lastRows[i].value;
      }
    }
    draft = null;
    fillModifiers(mods);
    var fields = el.settingsForm.elements;
    fields.SERVER_NAME.value = values.SERVER_NAME || "";
    fields.WORLD_NAME.value = values.WORLD_NAME || "";
    // Already the mask when one is stored -- the real password is never in this page.
    fields.SERVER_PASS.value = values.SERVER_PASS || "";
    fields.SERVER_PORT.value = values.SERVER_PORT || "";
    fields.SERVER_PUBLIC.checked = isOn(values.SERVER_PUBLIC);
    fields.CROSSPLAY.checked = isOn(values.CROSSPLAY);
    editing = true;
    syncSettingsControls();
    fields.SERVER_NAME.focus();
  }

  function closeEditor() {
    // Cancel and a completed save both mean the typed values are finished with.
    draft = null;
    editing = false;
    syncSettingsControls();
  }

  function collectSettings() {
    var fields = el.settingsForm.elements;
    return {
      SERVER_NAME: fields.SERVER_NAME.value,
      WORLD_NAME: fields.WORLD_NAME.value,
      SERVER_PASS: fields.SERVER_PASS.value,
      SERVER_PORT: fields.SERVER_PORT.value,
      SERVER_PUBLIC: fields.SERVER_PUBLIC.checked ? "1" : "0",
      CROSSPLAY: fields.CROSSPLAY.checked ? "true" : "false"
    };
  }

  function saveSettings() {
    var body = collectSettings();
    // A secret still showing its mask was not retyped, so the key is left out of the
    // request entirely and the stored value is never in play. The manager refuses the
    // mask on its own account too -- this just keeps it off the wire.
    if (body.SERVER_PASS === MASK) { delete body.SERVER_PASS; }
    system("settings save requested");
    // The modifiers go as fields, not as the composed string: the manager composes it
    // (ordering is correctness) and re-appends the arguments it does not manage.
    post("/api/settings", {
      settings: body,
      modifiers: collectModifiers()
    }).then(function (payload) {
      if (payload && payload.saved) {
        if (payload.message) { system(payload.message); }
        closeEditor();
      } else {
        // Refused: the form stays open, with the operator's values still in it, and the
        // banner names the offending field.
        syncSettingsControls();
      }
    });
  }

  function renderSettings(rows, settingsError) {
    if (rows) { lastRows = rows; }
    if (!el.settings || !rows) { return; }
    // Rebuilt every status push (~2s), this would destroy any text selection in the
    // panel the operator is reading values out of. Only touch the DOM on a change.
    // This is also why the editor is a separate form rather than inputs in the table:
    // a rebuild here must never take away what the operator is halfway through typing.
    var signature = JSON.stringify(rows);
    if (el.settings.getAttribute("data-signature") === signature) {
      if (settingsError) { showError(settingsError, "settings"); } else { clearErrorFrom("settings"); }
      return;
    }
    var html = "";
    for (var i = 0; i < rows.length; i++) {
      html += "<tr><th scope=\"row\"></th><td></td></tr>";
    }
    el.settings.innerHTML = html;
    var trs = el.settings.querySelectorAll("tr");
    for (var j = 0; j < rows.length; j++) {
      trs[j].children[0].textContent = rows[j].key;
      trs[j].children[1].textContent = rows[j].value;
    }
    el.settings.setAttribute("data-signature", signature);
    if (settingsError) { showError(settingsError, "settings"); } else { clearErrorFrom("settings"); }
  }

  function renderModifiers(mods) {
    if (!mods) { return; }
    lastModifiers = mods;
    // A closed editor follows the file. An open one is the operator's: refresh only the
    // preview, whose unmanaged tail comes from the file and is not theirs to edit, and
    // never their selections -- the same rule the settings table follows.
    if (editing) { renderModifierPreview(); } else { fillModifiers(mods); }
  }

  // -------------------------------------------------------------- websocket

  function wsUrl() {
    var scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    return scheme + "//" + window.location.host + "/ws/logs";
  }

  function setLink(state) {
    var map = {
      live: ["live", "badge-ready"],
      connecting: ["connecting…", "badge-busy"],
      down: ["disconnected", "badge-error"]
    };
    var entry = map[state] || map.down;
    el.link.textContent = entry[0];
    el.link.className = "badge " + entry[1];
  }

  function connect() {
    setLink("connecting");
    try {
      socket = new WebSocket(wsUrl());
    } catch (err) {
      scheduleReconnect();
      return;
    }

    socket.onopen = function () {
      backoff = RECONNECT_MIN_MS;
      setLink("live");
      if (everConnected) { system("reconnected, backfilling recent log"); }
      everConnected = true;
    };

    socket.onmessage = function (event) {
      var msg;
      try { msg = JSON.parse(event.data); } catch (err) { return; }
      if (msg.type === "status") {
        renderStatus(msg.status || {});
        renderSettings(msg.settings, msg.settings_error);
        renderModifiers(msg.modifiers);
      } else if (msg.type === "log") {
        for (var i = 0; i < msg.lines.length; i++) {
          var line = msg.lines[i];
          append(line, /game server connected|Ready for connections/i.test(line) ? "ready" : null);
        }
      } else if (msg.type === "log_error") {
        system(msg.error);
      }
    };

    socket.onclose = function (event) {
      socket = null;
      setLink("down");
      if (event && event.code === 1008) {
        // Session gone -- the login gate, not a transport hiccup.
        window.location.href = "/login";
        return;
      }
      scheduleReconnect();
    };

    socket.onerror = function () { setLink("down"); };
  }

  function scheduleReconnect() {
    var delay = backoff;
    backoff = Math.min(backoff * 2, RECONNECT_MAX_MS);
    setLink("down");
    window.setTimeout(connect, delay);
  }

  // ---------------------------------------------------------------- actions

  function post(path, body) {
    pendingAction = true;
    el.start.disabled = el.stop.disabled = el.restart.disabled = true;
    syncSettingsControls();
    clearError();
    return fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (response) {
      if (response.status === 401) {
        window.location.href = "/login";
        return null;
      }
      return response.json().catch(function () { return {}; }).then(function (payload) {
        if (!response.ok) {
          showError((payload.error || "Request failed.") +
            (payload.docker_error ? " — " + payload.docker_error : ""));
          if (payload.force_available) { forceOffered = true; }
        } else {
          forceOffered = false;
          el.force.hidden = true;
        }
        return payload;
      });
    }).catch(function (err) {
      showError("Could not reach the manager: " + err);
      return null;
    }).then(function (payload) {
      // Clear the flag BEFORE re-rendering, or every button stays disabled and a
      // failed action locks the operator out until a page reload.
      pendingAction = false;
      // A settings save answers with the file as it now stands, so the panel shows the
      // new values without waiting for the next push.
      if (payload && payload.settings) {
        renderSettings(payload.settings, payload.settings_error);
      }
      if (payload && payload.modifiers) {
        renderModifiers(payload.modifiers);
      }
      if (payload && payload.status) {
        renderStatus(payload.status);
      } else if (lastStatus) {
        renderStatus(lastStatus);
      } else {
        // Nothing to render from (manager unreachable and no push yet) -- hand the
        // controls back rather than leaving them dead.
        el.start.disabled = el.stop.disabled = el.restart.disabled = false;
        syncSettingsControls();
      }
      return payload;
    });
  }

  el.start.addEventListener("click", function () { system("start requested"); post("/api/start"); });
  el.stop.addEventListener("click", function () { system("stop requested"); post("/api/stop"); });
  el.restart.addEventListener("click", function () { system("restart requested"); post("/api/restart"); });
  el.force.addEventListener("click", function () {
    system("force stop requested");
    forceOffered = false;
    el.force.hidden = true;
    post("/api/stop", { force: true });
  });
  el.clear.addEventListener("click", function () { el.console.textContent = ""; });

  if (el.settingsForm && el.settingsEdit) {
    MASK = el.settingsForm.getAttribute("data-mask") || MASK;
    if (el.modifierFields) {
      // One listener on the fieldset: change events from every select and checkbox in
      // it bubble here, so adding a modifier to the markup needs no change in this file.
      el.modifierFields.addEventListener("change", renderModifierPreview);
      renderModifierPreview();
    }
    el.settingsEdit.addEventListener("click", openEditor);
    el.settingsCancel.addEventListener("click", function () {
      system("settings edit cancelled");
      closeEditor();
    });
    el.settingsForm.addEventListener("submit", function (event) {
      // Same-page fetch, not a form POST: the answer carries the refreshed status and
      // settings, and a refusal has to leave the typed values on screen.
      event.preventDefault();
      saveSettings();
    });
    syncSettingsControls();
  }

  connect();
})();
