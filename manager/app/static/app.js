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

  // Which dashboard tab was last chosen. Per-browser, not per-session: it is a view
  // preference, and the manager has no business storing it.
  var TAB_STORAGE_KEY = "valheim-manager.tab";
  // The tab holding the live log. Also the landing tab, but the two are separate
  // facts: the console needs re-following on the way in whether or not it is where
  // an unusable stored value would have landed.
  var CONSOLE_TAB = "console";
  var DEFAULT_TAB = CONSOLE_TAB;

  var el = {
    tablist: document.querySelector("[role='tablist']"),
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
    worldsBody: document.getElementById("worlds-body"),
    worldsTable: document.getElementById("worlds-table"),
    worldsEmpty: document.getElementById("worlds-empty"),
    worldsError: document.getElementById("worlds-error"),
    modsRefresh: document.getElementById("btn-mods-refresh"),
    modsError: document.getElementById("mods-error"),
    modsWorld: document.getElementById("mods-world"),
    modsWorldNote: document.getElementById("mods-world-note"),
    modsTable: document.getElementById("mods-table"),
    modsBody: document.getElementById("mods-body"),
    modsEmpty: document.getElementById("mods-empty"),
    modForm: document.getElementById("mod-upload-form"),
    modDropzone: document.getElementById("mod-dropzone"),
    modInput: document.getElementById("mod-file-input"),
    modPick: document.getElementById("btn-mod-pick"),
    modSelection: document.getElementById("mod-selection"),
    modName: document.getElementById("mod-upload-name"),
    modUpload: document.getElementById("btn-mod-upload"),
    modReset: document.getElementById("btn-mod-reset"),
    worldNewForm: document.getElementById("world-new-form"),
    worldNewName: document.getElementById("world-new-name"),
    worldNewButton: document.getElementById("btn-world-new"),
    deleteDialog: document.getElementById("delete-dialog"),
    deleteName: document.getElementById("delete-dialog-name"),
    deleteDetail: document.getElementById("delete-dialog-detail"),
    deleteConfirm: document.getElementById("btn-delete-confirm"),
    deleteCancel: document.getElementById("btn-delete-cancel"),
    worldsLocked: document.getElementById("worlds-locked"),
    worldsRefresh: document.getElementById("btn-worlds-refresh"),
    uploadForm: document.getElementById("world-upload-form"),
    dropzone: document.getElementById("world-dropzone"),
    folderInput: document.getElementById("world-folder-input"),
    fileInput: document.getElementById("world-file-input"),
    pickFolder: document.getElementById("btn-pick-folder"),
    pickFiles: document.getElementById("btn-pick-files"),
    selection: document.getElementById("world-selection"),
    uploadName: document.getElementById("world-upload-name"),
    uploadButton: document.getElementById("btn-world-upload"),
    uploadReset: document.getElementById("btn-world-reset"),
    uploadProgress: document.getElementById("world-progress"),
    legacyWarning: document.getElementById("world-legacy-warning"),
    start: document.getElementById("btn-start"),
    stop: document.getElementById("btn-stop"),
    restart: document.getElementById("btn-restart"),
    force: document.getElementById("btn-force"),
    clear: document.getElementById("btn-clear")
  };

  // What the operator sees on the badge. Plain words: "no container" and "pulling
  // image" are true but they describe Docker's world, not the player's.
  var PHASES = {
    absent:   { label: "off",          cls: "badge-stopped" },
    stopped:  { label: "off",          cls: "badge-stopped" },
    pulling:  { label: "downloading",  cls: "badge-busy" },
    creating: { label: "setting up",   cls: "badge-busy" },
    starting: { label: "starting",     cls: "badge-busy" },
    stopping: { label: "stopping",     cls: "badge-busy" },
    running:  { label: "loading world", cls: "badge-running" },
    ready:    { label: "ready",        cls: "badge-ready" },
    paused:   { label: "paused",       cls: "badge-stopped" },
    error:    { label: "problem",      cls: "badge-error" }
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
  // The worlds panel's own state: the last listing the manager sent, and the files the
  // operator has picked or dropped but not yet uploaded.
  var lastWorlds = null;
  var selection = [];
  var uploading = false;
  // The in-flight upload, so Clear can abort it.
  var uploadRequest = null;

  // ------------------------------------------------------------------- tabs
  //
  // Panels are SHOWN AND HIDDEN, never built or torn down. The console is a <pre>
  // holding up to MAX_CONSOLE_LINES appended lines, plus a scroll position and the
  // follow checkbox, and the WebSocket pump has no notion of tabs -- re-rendering on a
  // switch would throw all of that away. `hidden` on the panel leaves the buffer and
  // the socket exactly as they are, and nothing here ever issues a request: lock
  // state, error text and panel contents keep coming from the status push.
  //
  // The one thing hiding does NOT preserve is FOLLOWING. A display:none box reports
  // every scroll metric as 0, so the follow-scroll in append() is a no-op for lines
  // that arrive while another tab is up; selectTab re-establishes it on the way in.
  // A parked view (follow off) is left exactly where it was.

  // The tab buttons in document order, which is also the order the arrow keys walk.
  var tabButtons = [];

  function tabName(button) { return button.getAttribute("data-tab"); }

  function tabPanel(button) {
    return document.getElementById(button.getAttribute("aria-controls"));
  }

  // A tab is usable only if the panel it names is actually on the page: a stored name
  // from an older build, or one another app on this origin wrote, must land on the
  // Console rather than on three hidden panels and a blank screen.
  function findTab(name) {
    for (var i = 0; i < tabButtons.length; i++) {
      if (tabName(tabButtons[i]) === name && tabPanel(tabButtons[i])) {
        return tabButtons[i];
      }
    }
    return null;
  }

  // Storage is unavailable in some privacy modes and throws on read as well as write,
  // so neither may be allowed to take the dashboard down with it.
  function storedTab() {
    try { return window.localStorage.getItem(TAB_STORAGE_KEY); } catch (err) { return null; }
  }

  function storeTab(name) {
    try { window.localStorage.setItem(TAB_STORAGE_KEY, name); } catch (err) { /* ignore */ }
  }

  function selectTab(name, focus) {
    var chosen = findTab(name);
    if (!chosen) { return false; }
    for (var i = 0; i < tabButtons.length; i++) {
      var button = tabButtons[i];
      // findTab vouches for the CHOSEN tab's panel, not for the others'. One typo in
      // an aria-controls would otherwise throw here -- and this runs from initTabs,
      // where a throw takes the rest of the IIFE with it: no socket, no listeners,
      // Start disabled forever. Skipping a broken tab degrades; throwing does not.
      var panel = tabPanel(button);
      if (!panel) { continue; }
      var on = button === chosen;
      button.setAttribute("aria-selected", on ? "true" : "false");
      // One tab stop for the whole strip: the arrows move within it, Tab leaves it.
      button.tabIndex = on ? 0 : -1;
      panel.hidden = !on;
    }
    if (focus) { chosen.focus(); }
    // Mods are per world, so the list is only meaningful next to the current set of
    // worlds -- which a switch or a delete on another tab may just have changed.
    if (tabName(chosen) === "mods") { fillModWorlds(); refreshMods(el.modsWorld.value); }
    // A hidden console cannot scroll. Every scroll metric reads 0 on a display:none
    // box, so append()'s follow-scroll is a no-op for every line that arrives while
    // another tab is up, and the browser restores the OLD offset when the panel comes
    // back. Following therefore has to be re-established on the way in -- otherwise
    // the operator returns to a console whose checkbox says "follow" and whose view
    // is parked where they left it, or at the very top after a reload onto another
    // tab, and it stays there until the next line arrives. On a server that has gone
    // quiet -- which is what "ready" means -- that is indefinitely.
    if (tabName(chosen) === CONSOLE_TAB && el.follow.checked) { followTail(); }
    storeTab(name);
    return true;
  }

  // What a hidden panel cannot say for itself: that its controls are locked, or that it
  // is holding an error. Inside the button, so it is part of the tab's accessible name
  // ("Server settings, locked") rather than colour alone.
  function markTab(name, note, kind) {
    var button = findTab(name);
    var mark = button ? button.querySelector("[data-tab-note]") : null;
    if (!mark) { return; }
    mark.textContent = note || "";
    mark.className = "tab-note" + (note && kind ? " tab-note-" + kind : "");
    mark.hidden = !note;
  }

  function onTabKeydown(event) {
    var index = tabButtons.indexOf(event.target);
    if (index === -1) { return; }
    // Enter and Space activate whatever the arrows left the focus on. Handled here
    // rather than left to the button's own click: preventDefault is needed anyway to
    // stop Space scrolling the page, and that same preventDefault would suppress the
    // click. Selecting a tab twice is harmless, so a click that does arrive is fine.
    if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") {
      event.preventDefault();
      selectTab(tabName(tabButtons[index]), true);
      return;
    }
    var next;
    if (event.key === "ArrowLeft" || event.key === "Left") { next = index - 1; }
    else if (event.key === "ArrowRight" || event.key === "Right") { next = index + 1; }
    else if (event.key === "Home") { next = 0; }
    else if (event.key === "End") { next = tabButtons.length - 1; }
    else { return; }
    event.preventDefault();
    // A ring, which is what a tab list is: past the last tab is the first.
    next = (next + tabButtons.length) % tabButtons.length;
    selectTab(tabName(tabButtons[next]), true);
  }

  function initTabs() {
    if (!el.tablist) { return; }
    tabButtons = Array.prototype.slice.call(el.tablist.querySelectorAll("[role='tab']"));
    if (!tabButtons.length) { return; }
    el.tablist.addEventListener("click", function (event) {
      var button = event.target.closest ? event.target.closest("[role='tab']") : null;
      if (button) { selectTab(tabName(button), false); }
    });
    el.tablist.addEventListener("keydown", onTabKeydown);
    // The stored choice, or the Console -- for a missing value and for an unusable one
    // alike, since findTab refuses anything without a panel.
    if (!selectTab(storedTab(), false)) { selectTab(DEFAULT_TAB, false); }
  }

  // ---------------------------------------------------------------- console

  function atBottom() {
    return el.console.scrollHeight - el.console.scrollTop - el.console.clientHeight < 40;
  }

  // Pin the view to the newest line. Only has any effect while the panel is
  // visible, which is precisely why selectTab has to call it again on the way in.
  function followTail() { el.console.scrollTop = el.console.scrollHeight; }

  function append(text, cls) {
    var stick = el.follow.checked || atBottom();
    var node = document.createElement("span");
    if (cls) { node.className = cls; }
    node.textContent = text + "\n";
    el.console.appendChild(node);
    while (el.console.childNodes.length > MAX_CONSOLE_LINES) {
      el.console.removeChild(el.console.firstChild);
    }
    if (stick) { followTail(); }
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
    syncWorldControls();
  }

  // -------------------------------------------------------- settings editor

  // Why a panel is refused, or null when the server is off. The manager re-checks this
  // for itself when the request arrives -- this is only about not offering what would
  // be refused.
  function lockReason(status, readOnly, action) {
    if (OFF_PHASES[status.phase]) { return null; }
    if (status.phase === "error") {
      return "Cannot reach Docker, so there is no way to tell whether your server is" +
        " running. " + readOnly + " until that is sorted out.";
    }
    var label = (PHASES[status.phase] || PHASES.error).label;
    // One shape for every phase. "It is stopping right now" reads oddly next to
    // "stop it first", so the sentence leads with the rule instead.
    return "You can only " + action + " while the server is off. It is " + label +
      " right now.";
  }

  function settingsLockReason(status) {
    return lockReason(status, "Settings stay read-only", "change these settings");
  }

  function worldsLockReason(status) {
    return lockReason(status, "Worlds stay read-only", "switch or upload a world");
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
      system("settings editor closed — your changes are still here");
    }
    // Without rows there is nothing to prefill from, and an editor opened on a settings
    // error would offer to save six empty fields.
    var haveValues = !!(lastRows && lastRows.length);
    el.settingsEdit.disabled =
      !lastStatus || !haveValues || !!reason || pendingAction || editing;
    el.settingsLocked.textContent = reason || "";
    el.settingsLocked.hidden = !reason;
    // The strip carries the lock too, so it is not news only to whoever opens the tab.
    markTab("settings", reason ? "locked" : "", "locked");
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
      composed || "(nothing — everything is on Valheim's default)";
    el.modifierPreview.className = composed ? "snippet" : "snippet is-empty";
    if (el.modifierUnmanaged) {
      el.modifierUnmanaged.textContent = unmanagedArgs()
        ? "Kept as it is — these are extra options you set yourself, and this page " +
          "leaves them alone: " + unmanagedArgs()
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
      // The manager names each setting; a key it has no name for is shown as itself.
      trs[j].children[0].textContent = rows[j].label || rows[j].key;
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

  // ------------------------------------------------------------ worlds panel
  //
  // Listing is a fetch of its own rather than a status push: walking the volume every
  // couple of seconds, per open tab, would be a real cost for a list that only changes
  // when this panel changes it. Every world answer carries the whole panel back, so a
  // switch refreshes the settings table and the badge along with the list.

  function syncWorldControls() {
    if (!el.worldsBody) { return; }
    // No status yet means LOCKED, not unlocked: this panel's list is fetched before
    // the WebSocket connects, so defaulting the other way would leave Load and the
    // pickers live against a server that is running, until the first frame lands.
    var reason = lastStatus
      ? worldsLockReason(lastStatus)
      : "Waiting for the first status from the manager…";
    var busy = pendingAction || uploading;
    el.worldsLocked.textContent = reason || "";
    el.worldsLocked.hidden = !reason;
    // An error the panel is holding outranks the lock in the strip: the message itself
    // stays in the panel, untouched by any tab switch, but a hidden panel cannot
    // announce that it has one.
    if (!el.worldsError.hidden) {
      markTab("worlds", "error", "error");
    } else if (uploading) {
      // The progress bar and its abort are both inside this panel, so from any other
      // tab an upload in flight would otherwise be entirely invisible -- and Start is
      // still live, so the operator can press it and have the manager refuse them.
      markTab("worlds", "uploading", "busy");
    } else {
      markTab("worlds", reason ? "locked" : "", "locked");
    }
    var buttons = el.worldsBody.querySelectorAll("button[data-world]");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].disabled = !!reason || busy;
    }
    el.uploadButton.disabled = !!reason || busy || !selection.length;
    // Clear stays live while an upload is in flight: it is the abort.
    el.uploadReset.disabled = pendingAction && !uploading;
    el.pickFolder.disabled = !!reason || busy;
    el.pickFiles.disabled = !!reason || busy;
    el.uploadName.disabled = !!reason || busy;
    el.worldsRefresh.disabled = busy;
    var backups = el.worldsBody.querySelectorAll("button[data-backup-world]");
    for (var b = 0; b < backups.length; b++) {
      // Deliberately NOT gated on `reason`: backing up a running server is the case
      // this button exists for.
      backups[b].disabled = busy;
    }
    var deletes = el.worldsBody.querySelectorAll("button[data-delete-world]");
    for (var d = 0; d < deletes.length; d++) {
      // A row that came back refused keeps its own explanation and stays reachable;
      // the panel lock is a plain disable, because the reason is already on screen.
      if (deletes[d].getAttribute("data-refused")) { continue; }
      deletes[d].disabled = !!reason || busy;
    }
    if (el.worldNewName) {
      el.worldNewName.disabled = !!reason || busy;
      el.worldNewButton.disabled = !!reason || busy;
    }
    // An open confirmation is about a world the operator can no longer delete.
    if (reason && el.deleteDialog && el.deleteDialog.open) { closeDelete(); }
  }

  function renderWorlds(payload) {
    if (!el.worldsBody || !payload) { return; }
    lastWorlds = payload.worlds || [];
    if (payload.worlds_error) {
      el.worldsError.textContent = payload.worlds_error;
      el.worldsError.hidden = false;
    } else {
      el.worldsError.hidden = true;
      el.worldsError.textContent = "";
    }
    el.worldsBody.textContent = "";
    for (var i = 0; i < lastWorlds.length; i++) {
      el.worldsBody.appendChild(worldRow(lastWorlds[i]));
    }
    // The empty note and the table are alternatives; an unreadable volume is neither
    // (the error above says why the list is empty, and inviting an upload into a
    // directory the manager cannot read would just produce a second failure).
    var empty = !lastWorlds.length && !payload.worlds_error;
    el.worldsEmpty.hidden = !empty;
    el.worldsTable.hidden = !lastWorlds.length;
    syncWorldControls();
  }

  function worldRow(world) {
    var tr = document.createElement("tr");
    var name = document.createElement("td");
    name.className = "world-name";
    name.textContent = world.name;
    tr.appendChild(name);

    var layout = document.createElement("td");
    var tag = document.createElement("span");
    tag.className = world.legacy ? "tag tag-legacy" : "tag";
    tag.textContent = world.layout;
    layout.appendChild(tag);
    if (world.legacy) {
      var note = document.createElement("div");
      note.className = "muted small";
      note.textContent = "loading it converts it to 1.0, permanently";
      layout.appendChild(note);
    }
    tr.appendChild(layout);

    var size = document.createElement("td");
    size.textContent = world.size;
    tr.appendChild(size);

    var action = document.createElement("td");
    action.className = "world-action";
    if (world.active) {
      var active = document.createElement("span");
      active.className = "tag tag-active";
      active.textContent = "active";
      action.appendChild(active);
      // Deleting the world the server is set to load would leave WORLD_NAME pointing
      // at nothing, and the next Start would quietly build a brand-new world under
      // that name. The manager refuses it; the row says so before it is pressed.
      action.appendChild(backupButton(world));
      action.appendChild(deleteButton(world,
        "This is the world your server is set to load. Load a different world first " +
        "(or make a new one), then you can delete this one."));
    } else if (world.loadable === false) {
      // The manager would refuse this name if it were sent, so offering Load would
      // hand the operator a 400 about a name they never typed. Say why instead.
      var blocked = document.createElement("span");
      blocked.className = "muted small";
      blocked.textContent = world.unloadable || "cannot be selected";
      action.appendChild(blocked);
    } else {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "ghost";
      button.setAttribute("data-world", world.name);
      button.textContent = "Load";
      action.appendChild(button);
      action.appendChild(backupButton(world));
      action.appendChild(deleteButton(world, ""));
    }
    tr.appendChild(action);
    return tr;
  }

  // The only world control that stays live while the server is running: a backup
  // reads the world and writes somewhere else, so there is nothing to collide with.
  function backupButton(world) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "ghost";
    button.setAttribute("data-backup-world", world.name);
    button.textContent = "Backup";
    return button;
  }

  // Present on every row, refused on the active one. Hiding it there would leave the
  // operator hunting for a button that is simply not drawn.
  function deleteButton(world, refusal) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "ghost danger-quiet";
    button.setAttribute("data-delete-world", world.name);
    button.textContent = "Delete";
    if (!refusal) { return button; }

    // NOT `disabled`. A disabled button takes no pointer events and no focus, so the
    // hover that explains why it is greyed out would never fire and the keyboard
    // could never reach the explanation at all. aria-disabled says the same thing to
    // assistive tech; the click handler and the sync below both honour it.
    button.setAttribute("aria-disabled", "true");
    button.setAttribute("data-refused", refusal);
    var bubble = document.createElement("span");
    bubble.className = "hint-bubble hint-bubble-end";
    bubble.setAttribute("role", "tooltip");
    bubble.id = "refused-" + encodeURIComponent(world.name);
    bubble.textContent = refusal;
    bubble.hidden = true;
    button.setAttribute("aria-describedby", bubble.id);
    var wrap = document.createElement("span");
    wrap.className = "hint-anchor";
    wrap.appendChild(button);
    wrap.appendChild(bubble);
    return wrap;
  }

  // ------------------------------------------------------------------ hints

  // One bubble behaviour for every anchor on the page: shown while the anchor is
  // hovered or holds focus, hidden otherwise. Delegated, so rows rebuilt by a status
  // push get it without re-wiring.
  function hintFor(node) {
    var anchor = node && node.closest ? node.closest(".hint-anchor") : null;
    return anchor ? anchor.querySelector(".hint-bubble") : null;
  }

  function showHint(node, on) {
    var bubble = hintFor(node);
    if (bubble) { bubble.hidden = !on; }
  }

  function initHints() {
    document.addEventListener("mouseover", function (e) { showHint(e.target, true); });
    document.addEventListener("mouseout", function (e) {
      // Moving between the button and its own bubble is not leaving the anchor.
      var to = e.relatedTarget;
      if (to && hintFor(to) === hintFor(e.target)) { return; }
      showHint(e.target, false);
    });
    document.addEventListener("focusin", function (e) { showHint(e.target, true); });
    document.addEventListener("focusout", function (e) { showHint(e.target, false); });
    // Esc dismisses a bubble without moving focus, the way a tooltip should.
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") { return; }
      var open = document.querySelectorAll(".hint-bubble:not([hidden])");
      for (var i = 0; i < open.length; i++) { open[i].hidden = true; }
    });
  }

  // ------------------------------------------------------------------- mods
  //
  // Mods are per world and BepInEx has one plugins folder, so "which world" is part
  // of every request here. The panel defaults to the world the next Start would open
  // -- the one the operator almost always means -- and says so.

  var modWorld = "";
  var modFiles = [];

  function renderMods(payload) {
    if (!el.modsBody || !payload) { return; }
    modWorld = payload.world || "";
    el.modsError.textContent = payload.mods_error || "";
    el.modsError.hidden = !payload.mods_error;

    var mods = payload.mods || [];
    el.modsBody.innerHTML = "";
    for (var i = 0; i < mods.length; i++) {
      el.modsBody.appendChild(modRow(mods[i]));
    }
    el.modsEmpty.hidden = !!mods.length || !modWorld || !!payload.mods_error;
    el.modsTable.hidden = !mods.length;
    el.modsWorldNote.textContent = modWorld
      ? (modWorld === (lastStatus && lastStatus.active_world ? lastStatus.active_world : activeWorld())
          ? "This is the world your server loads next."
          : "Your server is set to load " + (activeWorld() || "another world") + ".")
      : "No world selected yet.";
    syncModControls();
  }

  function activeWorld() {
    // `lastWorlds` is null until the first /api/worlds answer, and the Mods tab can
    // be the tab the page OPENS on -- earlier than that.
    var known = lastWorlds || [];
    for (var i = 0; i < known.length; i++) {
      if (known[i].active) { return known[i].name; }
    }
    return "";
  }

  function modRow(mod) {
    var tr = document.createElement("tr");
    var name = document.createElement("td");
    name.className = "world-name";
    name.textContent = mod.name;
    if (!mod.enabled) {
      var off = document.createElement("span");
      off.className = "tag tag-legacy";
      off.textContent = "off";
      name.appendChild(document.createTextNode(" "));
      name.appendChild(off);
    }
    tr.appendChild(name);

    var size = document.createElement("td");
    size.textContent = mod.size;
    tr.appendChild(size);

    var action = document.createElement("td");
    action.className = "world-action";
    var toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "ghost";
    toggle.setAttribute("data-mod-toggle", mod.name);
    toggle.setAttribute("data-enable", mod.enabled ? "false" : "true");
    toggle.textContent = mod.enabled ? "Switch off" : "Switch on";
    action.appendChild(toggle);

    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "ghost danger-quiet";
    remove.setAttribute("data-mod-delete", mod.name);
    remove.textContent = "Delete";
    action.appendChild(remove);
    tr.appendChild(action);
    return tr;
  }

  function fillModWorlds() {
    if (!el.modsWorld) { return; }
    var known = lastWorlds || [];
    var chosen = el.modsWorld.value || modWorld;
    el.modsWorld.innerHTML = "";
    for (var i = 0; i < known.length; i++) {
      var option = document.createElement("option");
      option.value = known[i].name;
      option.textContent = known[i].name + (known[i].active ? " (loaded next)" : "");
      el.modsWorld.appendChild(option);
    }
    if (chosen) { el.modsWorld.value = chosen; }
  }

  function refreshMods(world) {
    if (!el.modsBody) { return Promise.resolve(null); }
    var query = world ? "?world=" + encodeURIComponent(world) : "";
    return fetch("/api/mods" + query, { credentials: "same-origin" }).then(function (response) {
      if (response.status === 401) { window.location.href = "/login"; return null; }
      return response.json().catch(function () { return null; });
    }).then(function (payload) {
      if (payload) { fillModWorlds(); renderMods(payload); }
      return payload;
    }).catch(function (err) {
      showError("Could not read the mods: " + err, "mods");
      return null;
    });
  }

  function syncModControls() {
    if (!el.modUpload) { return; }
    var busy = pendingAction;
    el.modUpload.disabled = busy || !modFiles.length || !modWorld;
    el.modPick.disabled = busy;
    el.modName.disabled = busy;
    el.modsRefresh.disabled = busy;
    el.modsWorld.disabled = busy;
    var buttons = el.modsBody.querySelectorAll("button[data-mod-toggle], button[data-mod-delete]");
    for (var i = 0; i < buttons.length; i++) { buttons[i].disabled = busy; }
    markTab("mods", el.modsError.hidden ? "" : "error", "error");
  }

  function setModSelection(files) {
    modFiles = files;
    el.modSelection.textContent = files.length
      ? (files.length === 1 ? files[0].name : files.length + " files")
      : "Nothing selected yet.";
    syncModControls();
  }

  function uploadMod() {
    if (!modFiles.length || !modWorld) { return; }
    var body = new FormData();
    body.append("world", modWorld);
    body.append("name", el.modName.value);
    for (var i = 0; i < modFiles.length; i++) {
      body.append("files", modFiles[i], modFiles[i].webkitRelativePath || modFiles[i].name);
    }
    pendingAction = true;
    syncModControls();
    system("adding a mod to " + modWorld);
    fetch("/api/mods/upload", {
      method: "POST", credentials: "same-origin", body: body
    }).then(function (response) {
      if (response.status === 401) { window.location.href = "/login"; return null; }
      return response.json().catch(function () { return {}; });
    }).then(function (payload) {
      pendingAction = false;
      if (!payload) { return; }
      if (payload.error) { showError(payload.error, "mods"); }
      else if (payload.message) { system(payload.message); clearErrorFrom("mods"); }
      setModSelection([]);
      el.modName.value = "";
      el.modInput.value = "";
      renderMods(payload);
    }).catch(function (err) {
      pendingAction = false;
      showError("The mod did not get through: " + err, "mods");
      syncModControls();
    });
  }

  function modAction(path, body, note) {
    pendingAction = true;
    syncModControls();
    system(note);
    fetch(path, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (response) {
      if (response.status === 401) { window.location.href = "/login"; return null; }
      return response.json().catch(function () { return {}; });
    }).then(function (payload) {
      pendingAction = false;
      if (!payload) { return; }
      if (payload.error) { showError(payload.error, "mods"); }
      else if (payload.message) { system(payload.message); clearErrorFrom("mods"); }
      renderMods(payload);
    }).catch(function (err) {
      pendingAction = false;
      showError("Lost contact with the manager: " + err, "mods");
      syncModControls();
    });
  }

  function initMods() {
    if (!el.modForm) { return; }
    el.modsRefresh.addEventListener("click", function () { refreshMods(el.modsWorld.value); });
    el.modsWorld.addEventListener("change", function () { refreshMods(el.modsWorld.value); });
    el.modPick.addEventListener("click", function () { el.modInput.click(); });
    el.modInput.addEventListener("change", function () {
      setModSelection(Array.prototype.slice.call(el.modInput.files || []));
    });
    el.modReset.addEventListener("click", function () {
      setModSelection([]);
      el.modName.value = "";
      el.modInput.value = "";
    });
    el.modForm.addEventListener("submit", function (event) {
      event.preventDefault();
      if (!el.modUpload.disabled) { uploadMod(); }
    });
    ["dragenter", "dragover"].forEach(function (name) {
      el.modDropzone.addEventListener(name, function (event) {
        event.preventDefault();
        el.modDropzone.classList.add("is-over");
      });
    });
    ["dragleave", "drop"].forEach(function (name) {
      el.modDropzone.addEventListener(name, function () {
        el.modDropzone.classList.remove("is-over");
      });
    });
    el.modDropzone.addEventListener("drop", function (event) {
      event.preventDefault();
      var transfer = event.dataTransfer;
      setModSelection(Array.prototype.slice.call((transfer && transfer.files) || []));
    });
    el.modsBody.addEventListener("click", function (event) {
      var toggle = event.target.closest
        ? event.target.closest("button[data-mod-toggle]") : null;
      if (toggle && !toggle.disabled) {
        modAction("/api/mods/toggle", {
          world: modWorld,
          name: toggle.getAttribute("data-mod-toggle"),
          enabled: toggle.getAttribute("data-enable") === "true"
        }, "switching " + toggle.getAttribute("data-mod-toggle"));
        return;
      }
      var remove = event.target.closest
        ? event.target.closest("button[data-mod-delete]") : null;
      if (remove && !remove.disabled) {
        modAction("/api/mods/delete", {
          world: modWorld, name: remove.getAttribute("data-mod-delete")
        }, "deleting mod " + remove.getAttribute("data-mod-delete"));
      }
    });
  }

  // ------------------------------------------------------- delete confirmation

  // The world the dialog is currently asking about. Read on confirm rather than bound
  // to the button, so a refresh that rebuilds the table underneath an open dialog
  // cannot retarget it at whatever row now sits in that position.
  var pendingDelete = null;

  function askDelete(name) {
    var world = null;
    for (var i = 0; i < lastWorlds.length; i++) {
      if (lastWorlds[i].name === name) { world = lastWorlds[i]; }
    }
    if (!world || !el.deleteDialog) { return; }
    pendingDelete = world.name;
    el.deleteName.textContent = world.name;
    el.deleteDetail.textContent =
      world.size + (world.files ? " in " + world.files + " file(s)" : "");
    if (el.deleteDialog.showModal) {
      el.deleteDialog.showModal();
    } else if (window.confirm(
        "Delete " + world.name + " for good? This cannot be undone from here.")) {
      // No <dialog> support: still ask, never delete straight off a click.
      confirmDelete();
    } else {
      // Declined. Clearing this matters: a world left pending is one a later confirm
      // would delete without ever having been asked about.
      pendingDelete = null;
    }
  }

  function closeDelete() {
    pendingDelete = null;
    if (el.deleteDialog && el.deleteDialog.close && el.deleteDialog.open) {
      el.deleteDialog.close();
    }
  }

  function backupWorld(name) {
    system("backing up " + name);
    post("/api/worlds/backup", { name: name }).then(function (payload) {
      if (payload && payload.backed_up && payload.message) { system(payload.message); }
    });
  }

  function confirmDelete() {
    var name = pendingDelete;
    closeDelete();
    if (!name) { return; }
    system("deleting world " + name);
    post("/api/worlds/delete", { name: name }).then(function (payload) {
      if (payload && payload.deleted && payload.message) { system(payload.message); }
    });
  }

  function createWorld() {
    var name = el.worldNewName.value.trim();
    if (!name) {
      showError("Give the new world a name.", "worlds");
      return;
    }
    system("making a new world called " + name);
    post("/api/worlds/new", { name: name }).then(function (payload) {
      if (!payload || !payload.saved) { return; }
      el.worldNewName.value = "";
      if (payload.message) { system(payload.message); }
      if (editing && el.settingsForm.elements.WORLD_NAME) {
        el.settingsForm.elements.WORLD_NAME.value = payload.active_world || name;
      }
    });
  }

  function refreshWorlds() {
    return fetch("/api/worlds", { credentials: "same-origin" }).then(function (response) {
      if (response.status === 401) { window.location.href = "/login"; return null; }
      return response.json().catch(function () { return null; });
    }).then(function (payload) {
      if (payload) { renderWorlds(payload); }
      return payload;
    }).catch(function (err) {
      showError("Could not read the list of worlds: " + err, "worlds");
      return null;
    });
  }

  function switchWorld(name) {
    system("switching to world " + name);
    post("/api/worlds/switch", { name: name }).then(function (payload) {
      if (!payload || !payload.saved) { return; }
      if (payload.message) { system(payload.message); }
      // An editor that was already open still holds the OLD world name, and saving it
      // would quietly switch back. The rest of the form is the operator's; this one
      // field is now theirs by way of this button, so it follows.
      if (editing && el.settingsForm.elements.WORLD_NAME) {
        el.settingsForm.elements.WORLD_NAME.value = payload.active_world || name;
      }
    });
  }

  // ------------------------------------------------------------ world upload

  function fileLeaf(path) {
    return path.split("/").pop().toLowerCase();
  }

  // Only a hint, shown before the upload so the conversion is not a surprise sprung
  // afterwards; the manager makes the same call on the files it actually receives and
  // repeats the warning in its answer.
  function looksLegacy(entries) {
    var db = false;
    var fwl = false;
    for (var i = 0; i < entries.length; i++) {
      var leaf = fileLeaf(entries[i].path);
      if (/\.(db2|fwl2)$/.test(leaf)) { return false; }
      if (/\.db$/.test(leaf)) { db = true; }
      if (/\.fwl$/.test(leaf)) { fwl = true; }
    }
    return db && fwl;
  }

  // Every size an operator reads is formatted by the manager and shipped in the
  // payload (`max_upload`, each world's `size`). There is deliberately no size
  // formatter in this file: two of them are two chances to state a limit that is not
  // the one being enforced.
  function maxUploadBytes() {
    return parseInt(el.uploadForm.getAttribute("data-max-bytes"), 10) || 0;
  }

  function maxUploadHuman() {
    return el.uploadForm.getAttribute("data-max-human") || "the limit";
  }

  function maxUploadFiles() {
    return parseInt(el.uploadForm.getAttribute("data-max-files"), 10) || 0;
  }

  function selectionBytes() {
    var total = 0;
    for (var i = 0; i < selection.length; i++) { total += selection[i].file.size; }
    return total;
  }

  // The name the manager will derive if the operator types none: the world's own
  // folder, or a lone file's base name. Only used to warn about a clash before the
  // upload -- the manager decides the real one.
  function impliedName() {
    if (el.uploadName.value.trim()) { return el.uploadName.value.trim(); }
    if (!selection.length) { return ""; }
    var first = selection[0].path;
    if (first.indexOf("/") !== -1) { return first.split("/")[0]; }
    return first.replace(/\.(zip|db|fwl)$/i, "");
  }

  function setSelection(entries) {
    selection = entries.filter(function (entry) {
      // Whatever the operating system slipped into the folder. The manager drops these
      // too; doing it here as well keeps the count the operator reads honest.
      var leaf = fileLeaf(entry.path);
      return leaf !== ".ds_store" && leaf !== "thumbs.db" && leaf !== "desktop.ini" &&
        entry.path.toLowerCase().indexOf("__macosx/") !== 0;
    });
    el.legacyWarning.hidden = !looksLegacy(selection);
    if (!selection.length) {
      el.selection.textContent = "Nothing selected yet.";
    } else {
      var folder = selection[0].path.indexOf("/") === -1
        ? "" : " (" + selection[0].path.split("/")[0] + ")";
      el.selection.textContent = selection.length === 1
        ? selection[0].path
        : selection.length + " files" + folder;
    }
    syncWorldControls();
  }

  function clearSelection() {
    el.uploadProgress.hidden = true;
    el.uploadProgress.value = 0;
    el.fileInput.value = "";
    el.folderInput.value = "";
    setSelection([]);
  }

  // A dropped directory is walked here rather than in the manager: the browser hands
  // over a tree, and each file's path within it is what tells the manager the world's
  // own folder name. `readEntries` returns a slice at a time, so it is called until it
  // comes back empty -- a world has more chunk files than one call will ever return.
  //
  // Failures are COUNTED rather than skipped. A world missing some of its chunk files
  // still satisfies the `_main.N.db2` + `_main.N.fwl2` shape check, so a walk that
  // quietly dropped what it could not read would upload a silently incomplete world --
  // which is worse than any refusal, because it looks like it worked.
  function readEntry(entry, prefix, failures) {
    return new Promise(function (resolve) {
      if (!entry) { failures.push(prefix + "(unreadable item)"); resolve([]); return; }
      if (entry.isFile) {
        entry.file(function (file) {
          resolve([{ file: file, path: prefix + entry.name }]);
        }, function () {
          failures.push(prefix + entry.name);
          resolve([]);
        });
        return;
      }
      if (!entry.isDirectory) {
        failures.push(prefix + entry.name);
        resolve([]);
        return;
      }
      var reader = entry.createReader();
      var children = [];
      var readBatch = function () {
        reader.readEntries(function (batch) {
          if (!batch.length) {
            Promise.all(children.map(function (child) {
              return readEntry(child, prefix + entry.name + "/", failures);
            })).then(function (lists) {
              resolve([].concat.apply([], lists));
            });
            return;
          }
          children = children.concat(Array.prototype.slice.call(batch));
          readBatch();
        }, function () {
          failures.push(prefix + entry.name + "/");
          resolve([]);
        });
      };
      readBatch();
    });
  }

  function onDrop(event) {
    event.preventDefault();
    el.dropzone.classList.remove("is-over");
    var reason = lastStatus ? worldsLockReason(lastStatus) : null;
    if (reason || uploading) {
      // Locked or busy: say so rather than filling the panel with files it will not
      // send, and leave whatever was already selected alone.
      showError(reason || "An upload is already in flight.", "worlds");
      return;
    }
    var transfer = event.dataTransfer;
    var failures = [];
    var jobs = [];
    var items = transfer.items;
    var i;
    var entry;
    if (items && items.length && items[0].webkitGetAsEntry) {
      // Must be called synchronously here: the items are emptied once this handler
      // returns, and a walk started afterwards would find nothing. Text and URLs are
      // not entries at all, and come back null.
      for (i = 0; i < items.length; i++) {
        entry = items[i].webkitGetAsEntry();
        if (entry) { jobs.push(readEntry(entry, "", failures)); }
      }
    }
    if (!jobs.length) {
      var plain = [];
      for (i = 0; i < transfer.files.length; i++) {
        plain.push({ file: transfer.files[i], path: transfer.files[i].name });
      }
      if (!plain.length) {
        // Dropped text, a link, or something the browser will not hand over as a file.
        // Keeping the current selection matters: silently emptying it looks like the
        // drop worked.
        showError("There were no files in that drop. Drop the world's folder, a .zip of " +
          "it, or a .db and .fwl pair.", "worlds");
        return;
      }
      setSelection(plain);
      return;
    }
    Promise.all(jobs).then(function (lists) {
      if (failures.length) {
        showError("The browser could not read " + failures.length + " item(s) in that " +
          "drop (for instance " + failures[0] + "), so nothing was selected: an " +
          "incomplete world would still look like a valid one. Try again, or zip the " +
          "world's folder and drop the .zip.", "worlds");
        return;
      }
      setSelection([].concat.apply([], lists));
    });
  }

  function fromInput(input) {
    var entries = [];
    for (var i = 0; i < input.files.length; i++) {
      var file = input.files[i];
      entries.push({ file: file, path: file.webkitRelativePath || file.name });
    }
    setSelection(entries);
  }

  // Everything the manager is certain to refuse, refused here first. The manager is
  // still the check that counts -- these only spare the operator pushing a gigabyte up
  // the wire to be told no at the far end.
  function refusalAhead() {
    var cap = maxUploadBytes();
    if (cap && selectionBytes() > cap) {
      return "That is past the " + maxUploadHuman() +
        " limit on one world upload. Nothing was uploaded.";
    }
    var limit = maxUploadFiles();
    if (limit && selection.length > limit) {
      return "That is " + selection.length + " files, and the manager accepts at most " +
        limit + " in one upload. Zip the world's folder and drop the .zip instead — " +
        "an archive is one file whatever the world's size. Nothing was uploaded.";
    }
    var wanted = impliedName().toLowerCase();
    for (var i = 0; wanted && lastWorlds && i < lastWorlds.length; i++) {
      if (lastWorlds[i].name.toLowerCase() === wanted) {
        return "A world called " + lastWorlds[i].name + " is already on the volume, " +
          "and the manager never overwrites one. Give this upload a different name " +
          "below. Nothing was uploaded.";
      }
    }
    return null;
  }

  function uploadWorld() {
    if (!selection.length || uploading) { return; }
    var ahead = refusalAhead();
    if (ahead) {
      showError(ahead, "worlds");
      return;
    }
    var body = new FormData();
    for (var i = 0; i < selection.length; i++) {
      // The third argument is the part's filename, and it is how each file's path
      // inside the dropped folder reaches the manager -- a browser would otherwise
      // send the bare name and the world's own folder name would be lost.
      body.append("files", selection[i].file, selection[i].path);
    }
    body.append("name", el.uploadName.value);

    uploading = true;
    clearError();
    el.uploadProgress.hidden = false;
    el.uploadProgress.value = 0;
    syncWorldControls();
    system("uploading " + selection.length + " file(s)");

    // Module scope, not a local: Clear aborts it, so a mistaken 900 MB drop does not
    // mean reloading the page and waiting for it to finish first.
    uploadRequest = new XMLHttpRequest();
    var request = uploadRequest;
    request.open("POST", "/api/worlds/upload");
    request.withCredentials = true;
    request.upload.onprogress = function (event) {
      if (event.lengthComputable) {
        el.uploadProgress.value = Math.round((event.loaded / event.total) * 100);
      }
    };
    request.onload = function () {
      uploading = false;
      uploadRequest = null;
      el.uploadProgress.hidden = true;
      if (request.status === 401) { window.location.href = "/login"; return; }
      var payload = null;
      try { payload = JSON.parse(request.responseText); } catch (err) { payload = null; }
      if (request.status >= 200 && request.status < 300) {
        if (payload && payload.message) { system(payload.message); }
        if (payload && payload.warning) { system(payload.warning); }
        clearSelection();
        el.uploadName.value = "";
      } else {
        showError((payload && payload.error) || "The upload was refused.", "worlds");
      }
      if (payload) {
        if (payload.settings) { renderSettings(payload.settings, payload.settings_error); }
        if (payload.worlds) { renderWorlds(payload); }
        if (payload.status) { renderStatus(payload.status); }
      }
      syncWorldControls();
    };
    request.onerror = function () {
      uploading = false;
      uploadRequest = null;
      el.uploadProgress.hidden = true;
      showError("The upload did not get through. Check your connection and try again.",
        "worlds");
      syncWorldControls();
    };
    request.onabort = function () {
      uploading = false;
      uploadRequest = null;
      el.uploadProgress.hidden = true;
      // Nothing is written until the whole body is in, so an aborted upload leaves the
      // volume untouched -- worth saying, since the operator just cancelled a transfer.
      system("upload cancelled — nothing was saved");
      syncWorldControls();
    };
    request.send(body);
  }

  function abortUpload() {
    if (uploadRequest) { uploadRequest.abort(); }
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
      if (everConnected) { system("reconnected — catching up on the log"); }
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
    // The worlds panel has to take the busy lock too, or two presses of Load race the
    // same write to the settings file.
    syncWorldControls();
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
      showError("Lost contact with the manager: " + err);
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
      // A world switch answers with the whole panel, list included; a start or stop
      // does not carry one and leaves the list as it is.
      if (payload && payload.worlds) {
        renderWorlds(payload);
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
        syncWorldControls();
      }
      return payload;
    });
  }

  initTabs();
  initHints();
  initMods();

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

  if (el.uploadForm && el.worldsBody) {
    el.worldsBody.addEventListener("click", function (event) {
      var button = event.target.closest
        ? event.target.closest("button[data-world]") : null;
      if (button && !button.disabled) { switchWorld(button.getAttribute("data-world")); }
      var save = event.target.closest
        ? event.target.closest("button[data-backup-world]") : null;
      if (save && !save.disabled) { backupWorld(save.getAttribute("data-backup-world")); }
      var remove = event.target.closest
        ? event.target.closest("button[data-delete-world]") : null;
      if (remove && !remove.disabled &&
          remove.getAttribute("aria-disabled") !== "true") {
        askDelete(remove.getAttribute("data-delete-world"));
      }
    });
    if (el.worldNewForm) {
      el.worldNewForm.addEventListener("submit", function (event) {
        event.preventDefault();
        if (!el.worldNewButton.disabled) { createWorld(); }
      });
    }
    if (el.deleteDialog) {
      el.deleteConfirm.addEventListener("click", confirmDelete);
      el.deleteCancel.addEventListener("click", closeDelete);
      // Esc closes a <dialog> on its own; make that clear the pending world too.
      el.deleteDialog.addEventListener("close", function () { pendingDelete = null; });
    }
    el.worldsRefresh.addEventListener("click", function () { refreshWorlds(); });
    el.pickFolder.addEventListener("click", function () { el.folderInput.click(); });
    el.pickFiles.addEventListener("click", function () { el.fileInput.click(); });
    el.folderInput.addEventListener("change", function () { fromInput(el.folderInput); });
    el.fileInput.addEventListener("change", function () { fromInput(el.fileInput); });
    el.uploadReset.addEventListener("click", function () {
      // Doubles as the abort: while an upload is in flight this is the only way out of
      // a mistaken multi-hundred-megabyte drop short of reloading the page.
      if (uploading) {
        abortUpload();
        return;
      }
      clearSelection();
      el.uploadName.value = "";
    });
    // The drop zone is in the tab order, so it has to do something when a keyboard
    // reaches it; the folder picker is the equivalent of dropping a folder on it.
    el.dropzone.addEventListener("keydown", function (event) {
      if (event.key !== "Enter" && event.key !== " " && event.key !== "Spacebar") {
        return;
      }
      if (event.target !== el.dropzone) { return; }
      event.preventDefault();
      if (!el.pickFolder.disabled) { el.folderInput.click(); }
    });
    el.uploadForm.addEventListener("submit", function (event) {
      event.preventDefault();
      uploadWorld();
    });
    // dragover must be cancelled or the browser navigates to the dropped file instead.
    ["dragenter", "dragover"].forEach(function (name) {
      el.dropzone.addEventListener(name, function (event) {
        event.preventDefault();
        el.dropzone.classList.add("is-over");
      });
    });
    el.dropzone.addEventListener("dragleave", function (event) {
      if (event.target === el.dropzone) { el.dropzone.classList.remove("is-over"); }
    });
    el.dropzone.addEventListener("drop", onDrop);
    // A file dropped anywhere else would otherwise replace the page with it, which
    // looks exactly like the upload having gone somewhere.
    ["dragover", "drop"].forEach(function (name) {
      window.addEventListener(name, function (event) {
        if (!el.dropzone.contains(event.target)) { event.preventDefault(); }
      });
    });
    setSelection([]);
    refreshWorlds();
  }

  connect();
})();
