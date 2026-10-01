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
    clock: document.getElementById("server-clock"),
    clockTime: document.getElementById("clock-time"),
    clockDate: document.getElementById("clock-date"),
    message: document.getElementById("status-message"),
    state: document.getElementById("meta-state"),
    started: document.getElementById("meta-started"),
    image: document.getElementById("meta-image"),
    banner: document.getElementById("banner"),
    console: document.getElementById("console"),
    consoleFilter: document.getElementById("console-filter"),
    consoleCopy: document.getElementById("btn-console-copy"),
    telUptime: document.getElementById("tel-uptime"),
    telCpu: document.getElementById("tel-cpu"),
    telMem: document.getElementById("tel-mem"),
    follow: document.getElementById("follow"),
    settings: document.getElementById("settings-table"),
    passPeek: document.getElementById("btn-pass-peek"),
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
    backupsRefresh: document.getElementById("btn-backups-refresh"),
    backupsError: document.getElementById("backups-error"),
    backupsTable: document.getElementById("backups-table"),
    backupsBody: document.getElementById("backups-body"),
    backupsEmpty: document.getElementById("backups-empty"),
    backupIntervalHours: document.getElementById("backup-interval-hours"),
    backupDailyTime: document.getElementById("backup-daily-time"),
    backupKeep: document.getElementById("backup-keep"),
    restoreDialog: document.getElementById("restore-dialog"),
    backupDeleteDialog: document.getElementById("backup-delete-dialog"),
    backupDeleteName: document.getElementById("backup-delete-dialog-name"),
    backupDeleteDetail: document.getElementById("backup-delete-dialog-detail"),
    backupDeleteConfirm: document.getElementById("btn-backup-delete-confirm"),
    backupDeleteCancel: document.getElementById("btn-backup-delete-cancel"),
    restoreName: document.getElementById("restore-dialog-name"),
    restoreDetail: document.getElementById("restore-dialog-detail"),
    restoreChoice: document.querySelector(".restore-choice"),
    restoreAsNew: document.getElementById("restore-as-new"),
    restoreNewName: document.getElementById("restore-new-name"),
    restoreOverwrite: document.getElementById("restore-overwrite"),
    restoreOverwriteWorld: document.getElementById("restore-overwrite-world"),
    restoreConfirm: document.getElementById("btn-restore-confirm"),
    restoreCancel: document.getElementById("btn-restore-cancel"),
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
    clear: document.getElementById("btn-clear"),
    playersRefresh: document.getElementById("btn-players-refresh"),
    playersError: document.getElementById("players-error"),
    playersEnvConflict: document.getElementById("players-env-conflict"),
    playersTable: document.getElementById("players-table"),
    playersBody: document.getElementById("players-body"),
    playersEmpty: document.getElementById("players-empty"),
    playersPanel: document.getElementById("panel-players"),
    playersFilters: document.getElementById("players-filters"),
    playersFilterEmpty: document.getElementById("players-filter-empty"),
    playersFilterEmptyText: document.getElementById("players-filter-empty-text"),
    playersRawButton: document.getElementById("btn-players-raw"),
    rawDialog: document.getElementById("raw-dialog"),
    rawClose: document.getElementById("btn-raw-close"),
    banDialog: document.getElementById("ban-dialog"),
    banName: document.getElementById("ban-dialog-name"),
    banDetail: document.getElementById("ban-dialog-detail"),
    banConfirm: document.getElementById("btn-ban-confirm"),
    banCancel: document.getElementById("btn-ban-cancel"),
    whitelistToggle: document.getElementById("whitelist-toggle"),
    whitelistNote: document.getElementById("whitelist-note"),
    permittedNote: document.getElementById("permitted-note"),
    whitelistDialog: document.getElementById("whitelist-dialog"),
    whitelistCount: document.getElementById("whitelist-dialog-count"),
    whitelistList: document.getElementById("whitelist-dialog-list"),
    whitelistConfirm: document.getElementById("btn-whitelist-confirm"),
    whitelistCancel: document.getElementById("btn-whitelist-cancel"),
    playerAddForm: document.getElementById("player-add-form"),
    playerAddKind: document.getElementById("player-add-kind"),
    playerAddId: document.getElementById("player-add-id"),
    playerAddButton: document.getElementById("btn-player-add")
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
    // The backups list is changed by things that happen outside this panel -- the
    // timer, and Back up on a world row -- so it is re-read on the way in rather
    // than left as it was whenever the tab was last open.
    if (tabName(chosen) === "worlds" && backupsLoaded) { refreshBackups(); }
    // The roster moves on its own -- the watcher records every join and leave -- so a
    // "last seen" read when the tab was last open is stale by the time it is back.
    if (tabName(chosen) === "players" && playersLoaded && !playersBusy) { refreshPlayers(); }
    // Who is online changes by the second, but only matters while it is on screen.
    if (tabName(chosen) === "players") { startOnlinePoll(); } else { stopOnlinePoll(); }
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

  // A log line, split into the columns the console shows: the server's own time, the
  // process that wrote it, and the message without the game's second timestamp.
  // Lines that do not look like that (the manager's own notes, say) are message-only.
  var ANSI_RE = /\x1b\[[0-9;]*[A-Za-z]|\^\[\[[0-9;]*m/g;
  var LINE_RE = /^([A-Z][a-z]{2}\s+\d{1,2}\s(\d{2}:\d{2}:\d{2}))\s+(?:supervisord:\s+)?([A-Za-z][\w.-]*)(?:\[\d+\])?:?\s?(.*)$/;
  var GAME_STAMP_RE = /^\d{2}\/\d{2}\/\d{4} \d{2}:\d{2}:\d{2}:\s*/;

  function parseLine(text) {
    var clean = String(text).replace(ANSI_RE, "");
    var match = LINE_RE.exec(clean);
    if (!match) { return { time: "", source: "", message: clean }; }
    return {
      time: match[2],
      source: match[3].replace(/^valheim-/, ""),
      message: match[4].replace(GAME_STAMP_RE, "")
    };
  }

  // Valheim's log has no levels of its own; these are read off the words.
  function lineLevel(message) {
    if (/\b(error|exception|failed|fatal)\b/i.test(message)) { return "error"; }
    if (/\bwarn(ing)?\b/i.test(message)) { return "warn"; }
    return "";
  }

  var consoleFilterText = "";

  function lineMatches(node) {
    return !consoleFilterText ||
      (node.getAttribute("data-raw") || "").toLowerCase().indexOf(consoleFilterText) !== -1;
  }

  function applyConsoleFilter() {
    consoleFilterText = (el.consoleFilter.value || "").trim().toLowerCase();
    var lines = el.console.children;
    for (var i = 0; i < lines.length; i++) {
      lines[i].classList.toggle("is-filtered", !lineMatches(lines[i]));
    }
  }

  function copyConsole() {
    var lines = el.console.children;
    var out = [];
    for (var i = 0; i < lines.length; i++) {
      if (!lines[i].classList.contains("is-filtered")) { out.push(lines[i].getAttribute("data-raw") || ""); }
    }
    copyText(out.join("\n"), el.consoleCopy);
  }

  function append(text, cls) {
    var stick = el.follow.checked || atBottom();
    var parts = parseLine(text);
    var node = document.createElement("span");
    var level = cls === "sys" ? "" : lineLevel(parts.message);
    node.className = "line" + (cls ? " " + cls : "") + (level ? " lvl-" + level : "");
    node.setAttribute("data-raw", String(text));
    var time = document.createElement("span");
    time.className = "t";
    time.textContent = parts.time;
    var source = document.createElement("span");
    source.className = "src";
    source.textContent = parts.source;
    var message = document.createElement("span");
    message.className = "m";
    message.textContent = parts.message;
    node.appendChild(time);
    node.appendChild(source);
    node.appendChild(message);
    node.appendChild(document.createTextNode("\n"));
    if (!lineMatches(node)) { node.classList.add("is-filtered"); }
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

  // The headline in words; any technical detail (a Docker exception, a traceback
  // fragment) folded away under Details rather than shouted across the page.
  function showError(text, source, detail) {
    el.banner.textContent = "";
    var headline = document.createElement("span");
    headline.textContent = text;
    el.banner.appendChild(headline);
    if (detail) {
      var more = document.createElement("details");
      more.className = "banner-detail";
      var summary = document.createElement("summary");
      summary.textContent = "Details";
      var pre = document.createElement("pre");
      pre.textContent = detail;
      more.appendChild(summary);
      more.appendChild(pre);
      el.banner.appendChild(more);
    }
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

  // ------------------------------------------------------------------ clock
  //
  // The server's wall clock, not the browser's. The manager sends an anchor -- an
  // instant plus the UTC offset in force at that instant -- and this ticks locally
  // in between: anchoring rather than polling keeps the clock off the network, and
  // re-anchoring on every status push keeps it from drifting away from the machine
  // it is reporting on.

  var DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  var MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  var clock = { epoch: null, offsetMinutes: 0, zone: "", takenAt: 0 };
  // The last strings written, so a 250ms tick costs a comparison rather than two
  // DOM writes a second that mostly change nothing.
  var clockPainted = { time: "", date: "" };

  function pad2(value) { return (value < 10 ? "0" : "") + value; }

  function anchorClock(payload) {
    if (!payload) { return; }
    var epoch = Number(payload.epoch);
    // A payload without a usable instant leaves the previous anchor alone: a clock
    // that keeps ticking from a slightly stale anchor beats one that blanks out.
    if (!isFinite(epoch)) { return; }
    clock.epoch = epoch;
    clock.offsetMinutes = Number(payload.offset_minutes) || 0;
    if (payload.zone) { clock.zone = String(payload.zone); }
    clock.takenAt = Date.now();
    paintClock();
  }

  function paintClock() {
    if (!el.clockTime || clock.epoch === null) { return; }
    // Shift the instant by the server's offset, then read it back with the UTC
    // getters. That yields the server's wall clock whatever zone the browser is in,
    // without this file knowing anything about time zones.
    var elapsed = (Date.now() - clock.takenAt) / 1000;
    var at = new Date((clock.epoch + elapsed + clock.offsetMinutes * 60) * 1000);

    var time = pad2(at.getUTCHours()) + ":" + pad2(at.getUTCMinutes()) + ":" + pad2(at.getUTCSeconds());
    var date = DAY_NAMES[at.getUTCDay()] + " " + at.getUTCDate() + " " +
               MONTH_NAMES[at.getUTCMonth()] + " " + at.getUTCFullYear();
    if (clock.zone) { date += " · " + clock.zone; }

    if (time !== clockPainted.time) {
      clockPainted.time = time;
      el.clockTime.textContent = time;
      // A machine-readable copy of what is on screen, offset included, so the
      // rendered time is never ambiguous about which zone it is in.
      var sign = clock.offsetMinutes < 0 ? "-" : "+";
      var abs = Math.abs(clock.offsetMinutes);
      el.clockTime.setAttribute(
        "datetime",
        at.getUTCFullYear() + "-" + pad2(at.getUTCMonth() + 1) + "-" + pad2(at.getUTCDate()) +
        "T" + time + sign + pad2(Math.floor(abs / 60)) + ":" + pad2(abs % 60)
      );
    }
    if (date !== clockPainted.date) {
      clockPainted.date = date;
      el.clockDate.textContent = date;
    }
  }

  function startClock() {
    if (!el.clock) { return; }
    // The first anchor is rendered into the markup, so the clock is right on load
    // rather than blank until the socket connects.
    anchorClock({
      epoch: el.clock.getAttribute("data-epoch"),
      offset_minutes: el.clock.getAttribute("data-offset"),
      zone: el.clockDate ? el.clockDate.textContent.trim() : ""
    });
    // Faster than once a second so the seconds roll over when they should rather
    // than up to a second late; the paint is skipped unless the text changed. A
    // background tab throttles this, and the next paint recomputes from Date.now(),
    // so coming back to the tab corrects itself with no extra bookkeeping.
    window.setInterval(paintClock, 250);
  }


  // ---------------------------------------------------------------- backups
  //
  // Three writers put archives in one folder and the table has to tell them apart:
  // the Backup button (MANUAL-), this timer (SCHEDULED-), and the game server's own
  // hourly ones. Only the first two are the manager's to delete, which the server
  // decides -- the row just draws what it was told.

  var BACKUP_KINDS = {
    manual: "You",
    scheduled: "Automatic",
    game: "Game server"
  };

  // The pending restore, between opening the dialog and confirming it.
  var restoring = null;
  var backupsLoaded = false;

  function backupLimit(name, fallback) {
    var limits = lastBackupLimits || {};
    var value = Number(limits[name]);
    return isFinite(value) && value > 0 ? value : fallback;
  }
  var lastBackupLimits = null;

  function showBackupsError(text) {
    if (!el.backupsError) { return; }
    el.backupsError.textContent = text;
    el.backupsError.hidden = !text;
  }

  // "3 days ago", measured against the server's clock so it agrees with the header.
  function formatAgo(epoch) {
    var value = Number(epoch);
    if (!isFinite(value) || value <= 0) { return "—"; }
    if (clock.epoch === null) { return formatTaken(value); }
    var now = clock.epoch + (Date.now() - clock.takenAt) / 1000;
    var seconds = Math.max(0, now - value);
    if (seconds < 60) { return "just now"; }
    if (seconds < 3600) { return Math.floor(seconds / 60) + " min ago"; }
    if (seconds < 86400) { return Math.floor(seconds / 3600) + " h ago"; }
    var days = Math.floor(seconds / 86400);
    if (days < 30) { return days === 1 ? "yesterday" : days + " days ago"; }
    return formatTaken(value);
  }

  function formatTaken(epoch) {
    var value = Number(epoch);
    if (!isFinite(value) || value <= 0) { return "—"; }
    // The server's clock and offset, so a backup's time reads the same as the header
    // clock rather than being silently converted to the viewer's zone.
    var at = new Date((value + clock.offsetMinutes * 60) * 1000);
    return DAY_NAMES[at.getUTCDay()] + " " + at.getUTCDate() + " " +
      MONTH_NAMES[at.getUTCMonth()] + " " + pad2(at.getUTCHours()) + ":" +
      pad2(at.getUTCMinutes());
  }

  function renderBackups(payload) {
    if (!el.backupsBody) { return; }
    backupsLoaded = true;
    if (payload.limits) { lastBackupLimits = payload.limits; }
    showBackupsError(payload.error || "");
    if (payload.message) { system(payload.message); }
    if (payload.warning) { system(payload.warning); }
    if (payload.schedule) { renderSchedule(payload.schedule, payload.loaded_world || ""); }

    var rows = payload.backups || [];
    lastBackupRows = rows;
    el.backupsBody.textContent = "";
    el.backupsTable.hidden = rows.length === 0;
    el.backupsEmpty.hidden = rows.length !== 0;

    for (var i = 0; i < rows.length; i++) {
      el.backupsBody.appendChild(backupRow(rows[i]));
    }
  }

  function backupRow(backup) {
    var row = document.createElement("tr");

    var world = document.createElement("td");
    world.className = "backup-world";
    // The game's own archives are named by timestamp, not by world, and one can hold
    // several worlds. The server reads which from the archive's file list and sends
    // them as `contains`; when it could name none, the cell says so rather than
    // inventing one.
    world.textContent = backup.world || backupContents(backup) || "—";
    row.appendChild(world);

    var taken = document.createElement("td");
    taken.className = "backup-taken";
    // How long ago, with the exact time (on the server's clock) on hover.
    taken.textContent = formatAgo(backup.taken_at);
    taken.title = formatTaken(backup.taken_at);
    row.appendChild(taken);

    var by = document.createElement("td");
    var tag = document.createElement("span");
    tag.className = "tag" + (backup.kind === "manual" ? " tag-active" : "");
    tag.textContent = BACKUP_KINDS[backup.kind] || backup.kind;
    by.appendChild(tag);
    row.appendChild(by);

    var size = document.createElement("td");
    size.textContent = backup.size || "";
    row.appendChild(size);

    var action = document.createElement("td");
    action.className = "backup-action";
    if (backup.restorable) {
      var restore = document.createElement("button");
      restore.className = "ghost";
      restore.textContent = "Restore";
      restore.setAttribute("data-backup-restore", backup.name);
      action.appendChild(restore);
    }
    // A plain link: the browser does the download, and a session cookie rides along.
    var download = document.createElement("a");
    download.className = "button-link ghost small";
    download.href = "/api/backups/download?name=" + encodeURIComponent(backup.name);
    download.setAttribute("download", backup.name);
    download.textContent = "Download";
    action.appendChild(download);
    if (backup.deletable) {
      var remove = document.createElement("button");
      remove.className = "ghost danger-quiet";
      remove.textContent = "Delete";
      remove.setAttribute("data-backup-delete", backup.name);
      action.appendChild(remove);
    }
    // Every row gets Delete, the game server's own archives included: the image only
    // prunes those when it takes a new one, so on a server that has been off they stay
    // until someone removes them. The click asks first -- see askBackupDelete.
    row.appendChild(action);
    return row;
  }

  // Every schedule form on the page (today: the Worlds tab's), found by its id prefix.
  var scheduleForms = [];
  // The last schedule the manager sent, for the read-only card in Server settings.
  var lastSchedule = null;
  var lastScheduleWorld = "";

  function scheduleForm(form) {
    var p = form.id.replace(/backup-schedule-form$/, "");
    function byId(id) { return document.getElementById(p + id); }
    return {
      form: form,
      enabled: byId("backup-enabled"),
      fields: byId("backup-interval-fields"),
      everyDay: byId("backup-every-day"),
      everyCustom: byId("backup-every-custom"),
      everyAt: byId("backup-every-at"),
      hours: byId("backup-interval-hours"),
      dailyTime: byId("backup-daily-time"),
      keep: byId("backup-keep"),
      note: byId("backup-schedule-note"),
      save: byId("btn-backup-schedule-save")
    };
  }

  function renderSchedule(schedule, loadedWorld) {
    var hours = Number(schedule.interval_hours) || backupLimit("default_interval_hours", 24);
    var isDefault = hours === backupLimit("default_interval_hours", 24);
    // The mode decides which radio is on; the interval only decides which of the two
    // interval radios it is. Both settings are carried whichever is in force, so
    // switching away and back finds the other where it was left.
    var daily = schedule.mode === backupLimit("mode_daily", "daily");

    var note = "";
    if (!schedule.enabled) {
      note = "Automatic backups are off. Backups already saved are kept.";
    } else if (schedule.last_error) {
      // The timer ran and had nothing to do, or could not do it. Said plainly so a
      // timer that looks stuck is explained rather than mysterious.
      note = schedule.last_error;
    } else if (schedule.last_run_at) {
      note = "Last automatic backup: " + formatTaken(schedule.last_run_at) +
        (loadedWorld ? " (" + loadedWorld + ")" : "");
    } else {
      note = "The first automatic backup will be taken shortly.";
    }

    lastSchedule = schedule;
    lastScheduleWorld = loadedWorld;
    fillBackupCard();

    for (var i = 0; i < scheduleForms.length; i++) {
      var c = scheduleForms[i];
      c.enabled.checked = !!schedule.enabled;
      c.everyAt.checked = daily;
      c.everyDay.checked = !daily && isDefault;
      c.everyCustom.checked = !daily && !isDefault;
      c.hours.value = hours;
      c.dailyTime.value = schedule.daily_time ||
        backupLimit("default_daily_time", "03:00");
      c.keep.value = Number(schedule.keep_per_world) || backupLimit("default_keep", 7);
      c.note.textContent = note;
      syncScheduleControls(c);
    }
  }

  // Server settings' Backups card: the Worlds tab's schedule, as words.
  function fillBackupCard() {
    var auto = document.getElementById("settings-backup-auto");
    if (!auto || !lastSchedule) { return; }
    var s = lastSchedule;
    auto.textContent = "";
    var pill = document.createElement("span");
    pill.className = "flag " + (s.enabled ? "flag-on" : "flag-off");
    pill.textContent = s.enabled ? "On" : "Off";
    auto.appendChild(pill);
    var hours = Number(s.interval_hours) || backupLimit("default_interval_hours", 24);
    document.getElementById("settings-backup-when").textContent =
      s.mode === backupLimit("mode_daily", "daily")
        ? "Daily at " + (s.daily_time || backupLimit("default_daily_time", "03:00"))
        : "Every " + hours + " h";
    document.getElementById("settings-backup-keep").textContent =
      (Number(s.keep_per_world) || backupLimit("default_keep", 7)) + " per world";
    document.getElementById("settings-backup-last").textContent = s.last_run_at
      ? formatTaken(s.last_run_at) + (lastScheduleWorld ? " (" + lastScheduleWorld + ")" : "")
      : "none yet";
  }

  function syncScheduleControls(c) {
    var on = c.enabled.checked;
    c.form.classList.toggle("is-off", !on);
    c.fields.classList.toggle("is-default", c.everyDay.checked);
    c.fields.classList.toggle("is-daily", c.everyAt.checked);
    // Disabled rather than hidden: the value still says what would happen, and a
    // hidden control that reappears where you were not looking is worse.
    c.everyDay.disabled = !on;
    c.everyCustom.disabled = !on;
    c.everyAt.disabled = !on;
    c.hours.disabled = !on;
    c.dailyTime.disabled = !on;
    c.keep.disabled = !on;
  }

  function initScheduleForm(c) {
    var sync = function () { syncScheduleControls(c); };
    c.enabled.addEventListener("change", sync);
    c.everyDay.addEventListener("change", sync);
    c.everyCustom.addEventListener("change", sync);
    c.everyAt.addEventListener("change", sync);
    // Setting the time is how most people will pick "every day at", so treat it as
    // that rather than making them find the radio first -- the same courtesy the
    // hours field gets below.
    c.dailyTime.addEventListener("focus", function () {
      if (!c.dailyTime.disabled) { c.everyAt.checked = true; sync(); }
    });
    // Typing in the field is how most people will pick "custom", so treat it as that
    // rather than making them find the radio first.
    c.hours.addEventListener("focus", function () {
      if (!c.hours.disabled) { c.everyCustom.checked = true; sync(); }
    });
    c.form.addEventListener("submit", function (event) {
      event.preventDefault();
      var hours = c.everyDay.checked
        ? backupLimit("default_interval_hours", 24)
        : Number(c.hours.value);
      // Both settings go every time, whichever radio is on: the manager keeps the
      // one not in force so switching modes and back does not lose it.
      backupAction("/api/backups/schedule", {
        enabled: c.enabled.checked,
        mode: c.everyAt.checked
          ? backupLimit("mode_daily", "daily")
          : backupLimit("mode_interval", "interval"),
        interval_hours: hours,
        daily_time: c.dailyTime.value,
        keep_per_world: Number(c.keep.value)
      });
    });
  }

  function refreshBackups() {
    fetch("/api/backups", { credentials: "same-origin" })
      .then(function (response) {
        if (response.status === 401) { window.location.href = "/login"; return null; }
        return response.json();
      })
      .then(function (payload) { if (payload) { renderBackups(payload); } })
      .catch(function (err) {
        showBackupsError("Could not read the backups: " + err);
      });
  }

  function backupAction(url, body) {
    setBackupsBusy(true);
    fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    })
      .then(function (response) {
        if (response.status === 401) { window.location.href = "/login"; return null; }
        return response.json();
      })
      .then(function (payload) {
        setBackupsBusy(false);
        if (payload) { renderBackups(payload); }
      })
      .catch(function (err) {
        setBackupsBusy(false);
        showBackupsError("Lost contact with the manager: " + err);
      });
  }

  function setBackupsBusy(busy) {
    if (!el.backupsBody) { return; }
    var buttons = el.backupsBody.querySelectorAll("button");
    for (var i = 0; i < buttons.length; i++) { buttons[i].disabled = busy; }
    if (el.backupsRefresh) { el.backupsRefresh.disabled = busy; }
    for (var j = 0; j < scheduleForms.length; j++) { scheduleForms[j].save.disabled = busy; }
  }

  // ------------------------------------------------------------ the restore ask

  function openRestore(name, row) {
    restoring = { name: name, world: row ? row.world : "" };
    el.restoreName.textContent = name;
    el.restoreDetail.textContent = row
      ? (row.world ? "Taken from " + row.world + " on " : "Taken ") + formatTaken(row.taken_at) +
        (row.size ? " · " + row.size : "")
      : "";
    el.restoreOverwriteWorld.textContent = restoring.world || "the world it came from";
    // Overwriting is only offered when we know which world to overwrite.
    el.restoreOverwrite.disabled = !restoring.world;
    el.restoreAsNew.checked = true;
    el.restoreNewName.value = suggestRestoreName(restoring.world);
    syncRestoreChoice();
    if (typeof el.restoreDialog.showModal === "function") {
      el.restoreDialog.showModal();
    } else {
      el.restoreDialog.setAttribute("open", "open");
    }
  }

  function suggestRestoreName(world) {
    if (!world) { return ""; }
    // A name that will not collide, so the safe option works on the first press.
    var base = world + " restored";
    // Read off the rendered worlds table rather than a data attribute: the Load
    // button carries `data-world`, but the row for the world already loaded has no
    // Load button, so keying on that would miss exactly the name most likely to clash.
    var taken = {};
    var cells = el.worldsBody ? el.worldsBody.querySelectorAll("td.world-name") : [];
    for (var i = 0; i < cells.length; i++) {
      taken[(cells[i].textContent || "").trim().toLowerCase()] = 1;
    }
    if (!taken[base.toLowerCase()]) { return base; }
    for (var n = 2; n < 100; n++) {
      if (!taken[(base + " " + n).toLowerCase()]) { return base + " " + n; }
    }
    return base;
  }

  function syncRestoreChoice() {
    var overwrite = el.restoreOverwrite.checked;
    el.restoreChoice.classList.toggle("is-overwrite", overwrite);
    el.restoreNewName.disabled = overwrite;
    el.restoreConfirm.classList.toggle("is-destructive", overwrite);
    el.restoreConfirm.textContent = overwrite ? "Replace the world" : "Restore";
  }

  function closeRestore() {
    restoring = null;
    if (typeof el.restoreDialog.close === "function") {
      el.restoreDialog.close();
    } else {
      el.restoreDialog.removeAttribute("open");
    }
  }

  function initBackups() {
    if (!el.backupsBody) { return; }
    lastBackupLimits = {
      min_interval_hours: Number(el.backupIntervalHours.getAttribute("min")) || 1,
      max_interval_hours: Number(el.backupIntervalHours.getAttribute("max")) || 720,
      default_interval_hours: Number(el.backupIntervalHours.getAttribute("value")) || 24,
      min_keep: Number(el.backupKeep.getAttribute("min")) || 1,
      max_keep: Number(el.backupKeep.getAttribute("max")) || 200,
      default_keep: Number(el.backupKeep.getAttribute("value")) || 7,
      // Read off the rendered control rather than repeated here, so the default
      // cannot drift from the one the manager actually stores.
      default_daily_time: el.backupDailyTime.getAttribute("value") || "03:00"
    };

    el.backupsRefresh.addEventListener("click", refreshBackups);
    var forms = document.querySelectorAll("[data-schedule-form]");
    for (var f = 0; f < forms.length; f++) {
      scheduleForms.push(scheduleForm(forms[f]));
      initScheduleForm(scheduleForms[scheduleForms.length - 1]);
    }

    el.backupsBody.addEventListener("click", function (event) {
      var target = event.target.closest ? event.target : null;
      if (!target) { return; }
      var restore = target.closest("button[data-backup-restore]");
      if (restore && !restore.disabled) {
        var name = restore.getAttribute("data-backup-restore");
        openRestore(name, findBackupRow(name));
        return;
      }
      var remove = target.closest("button[data-backup-delete]");
      if (remove && !remove.disabled) {
        askBackupDelete(remove.getAttribute("data-backup-delete"));
      }
    });

    if (el.backupDeleteDialog) {
      el.backupDeleteConfirm.addEventListener("click", confirmBackupDelete);
      el.backupDeleteCancel.addEventListener("click", closeBackupDelete);
      el.backupDeleteDialog.addEventListener("close", function () {
        pendingBackupDelete = null;
      });
    }

    el.restoreAsNew.addEventListener("change", syncRestoreChoice);
    el.restoreOverwrite.addEventListener("change", syncRestoreChoice);
    el.restoreCancel.addEventListener("click", closeRestore);
    el.restoreDialog.addEventListener("cancel", function (event) {
      event.preventDefault();
      closeRestore();
    });
    el.restoreConfirm.addEventListener("click", function () {
      if (!restoring) { return; }
      var overwrite = el.restoreOverwrite.checked;
      var body = { name: restoring.name, overwrite: overwrite };
      if (!overwrite) { body.target = el.restoreNewName.value.trim(); }
      closeRestore();
      backupAction("/api/backups/restore", body);
    });

    refreshBackups();
  }

  // The rows the table last drew, so the dialog can name what it is about to do
  // without asking the server again.
  var lastBackupRows = [];
  // The worlds a game-server archive holds, as one line, or "" when none were named.
  function backupContents(backup) {
    var contains = backup && backup.contains;
    return contains && contains.length ? contains.join(", ") : "";
  }

  // The archive the delete dialog is asking about. Read on confirm rather than bound to
  // the button, so a refresh that rebuilds the table underneath an open dialog cannot
  // retarget it at whatever row now sits in that position.
  var pendingBackupDelete = null;

  function askBackupDelete(name) {
    var backup = findBackupRow(name);
    if (!backup) { return; }
    var worlds = backup.world || backupContents(backup);
    var detail = formatTaken(backup.taken_at) + (backup.size ? " · " + backup.size : "") +
      (worlds ? " · holds " + worlds : "");
    pendingBackupDelete = backup.name;
    if (el.backupDeleteDialog && el.backupDeleteDialog.showModal) {
      el.backupDeleteName.textContent = backup.name;
      el.backupDeleteDetail.textContent = detail;
      el.backupDeleteDialog.showModal();
    } else if (window.confirm(
        "Delete the backup " + backup.name + " (" + detail + ") for good? " +
        "This cannot be undone from here.")) {
      // No <dialog> support: still ask, never delete straight off a click.
      confirmBackupDelete();
    } else {
      // Declined. A backup left pending is one a later confirm would delete without
      // ever having been asked about.
      pendingBackupDelete = null;
    }
  }

  function confirmBackupDelete() {
    var name = pendingBackupDelete;
    closeBackupDelete();
    if (name) { backupAction("/api/backups/delete", { name: name }); }
  }

  function closeBackupDelete() {
    pendingBackupDelete = null;
    if (el.backupDeleteDialog && el.backupDeleteDialog.close && el.backupDeleteDialog.open) {
      el.backupDeleteDialog.close();
    }
  }

  function findBackupRow(name) {
    for (var i = 0; i < lastBackupRows.length; i++) {
      if (lastBackupRows[i].name === name) { return lastBackupRows[i]; }
    }
    return null;
  }

  // ----------------------------------------------------------------- status

  function renderStatus(status) {
    lastStatus = status;
    paintBadge();
    el.message.textContent = status.message || "";
    el.state.textContent = status.container_state || (status.container_exists ? "?" : "no container");
    // On the server's clock, like every other time on the page.
    var startedEpoch = startedAt(status);
    el.started.textContent = startedEpoch ? formatTaken(startedEpoch) : "–";
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
      showError(status.error, "status", status.docker_error || "");
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
    // Never leave a password showing for the next time the editor opens.
    var pass = document.getElementById("field-SERVER_PASS");
    if (pass && pass.type !== "password") { togglePassPeek(); }
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

  function togglePassPeek() {
    var field = document.getElementById("field-SERVER_PASS");
    if (!field || !el.passPeek) { return; }
    var show = field.type === "password";
    field.type = show ? "text" : "password";
    el.passPeek.textContent = show ? "Hide" : "Show";
    el.passPeek.setAttribute("aria-pressed", show ? "true" : "false");
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
    // One card per group, in the manager's group order; within a group, the file's
    // own order. A row from a manager too old to send a group lands under System.
    el.settings.textContent = "";
    for (var g = 0; g < SETTINGS_GROUP_ORDER.length; g++) {
      var name = SETTINGS_GROUP_ORDER[g];
      var members = rows.filter(function (row) { return (row.group || "System") === name; });
      if (!members.length) { continue; }
      var card = document.createElement("section");
      card.className = "set-group";
      var heading = document.createElement("h3");
      heading.className = "section-tag";
      heading.textContent = name;
      card.appendChild(heading);
      var table = document.createElement("table");
      table.className = "kv-table";
      var body = document.createElement("tbody");
      for (var j = 0; j < members.length; j++) { body.appendChild(settingRow(members[j])); }
      table.appendChild(body);
      card.appendChild(table);
      el.settings.appendChild(card);
    }
    el.settings.setAttribute("data-signature", signature);
    if (settingsError) { showError(settingsError, "settings"); } else { clearErrorFrom("settings"); }
  }

  var SETTINGS_GROUP_ORDER = ["Server", "World", "Updates", "System"];

  function settingRow(row) {
    var tr = document.createElement("tr");
    var th = document.createElement("th");
    th.scope = "row";
    // The manager names each setting; a key it has no name for is shown as itself.
    th.textContent = row.label || row.key;
    var td = document.createElement("td");
    if (row.flag === true || row.flag === false) {
      var pill = document.createElement("span");
      pill.className = "flag " + (row.flag ? "flag-on" : "flag-off");
      pill.textContent = row.flag ? "On" : "Off";
      pill.title = "Stored as " + row.value;
      td.appendChild(pill);
    } else if (row.secret && !row.value) {
      td.className = "is-unset";
      td.textContent = "not set";
    } else if (row.key === "SERVER_ARGS" && !row.value) {
      td.className = "is-unset";
      td.textContent = "none, Valheim's defaults";
    } else {
      td.textContent = row.value;
    }
    tr.appendChild(th);
    tr.appendChild(td);
    return tr;
  }

  // ------------------------------------------------------------ telemetry

  var TELEMETRY_MS = 5000;

  function startedAt(status) {
    if (!status || !status.started_at || status.started_at.indexOf("0001-01-01") === 0) { return 0; }
    var ms = Date.parse(status.started_at);
    return isFinite(ms) ? ms / 1000 : 0;
  }

  function serverUp() {
    return !!lastStatus && (lastStatus.phase === "running" || lastStatus.phase === "ready");
  }

  function consoleVisible() {
    var panel = document.getElementById("panel-console");
    return !!panel && !panel.hidden;
  }

  function formatSpan(seconds) {
    var s = Math.max(0, Math.floor(seconds));
    var h = Math.floor(s / 3600);
    var m = Math.floor((s % 3600) / 60);
    return (h ? h + "h " : "") + pad2(m) + "m " + pad2(s % 60) + "s";
  }

  function paintUptime() {
    if (!el.telUptime) { return; }
    var started = startedAt(lastStatus);
    if (!serverUp() || !started || clock.epoch === null) { el.telUptime.textContent = "–"; return; }
    var now = clock.epoch + (Date.now() - clock.takenAt) / 1000;
    el.telUptime.textContent = formatSpan(now - started);
  }

  function gigabytes(bytes) { return (bytes / 1073741824).toFixed(1); }

  function renderTelemetry(numbers) {
    var ok = numbers && numbers.available === true;
    el.telCpu.textContent = ok && typeof numbers.cpu_percent === "number"
      ? Math.round(numbers.cpu_percent) + "%" : "–";
    el.telMem.textContent = ok && numbers.memory_used !== null && numbers.memory_limit
      ? gigabytes(numbers.memory_used) + " / " + gigabytes(numbers.memory_limit) + " GB" : "–";
  }

  // One page-wide timer rather than one started by a tab switch: a switch must never
  // call the manager. Each tick fetches only while the Console is on screen and the
  // server is up, so nobody pays for a stats sample they are not looking at.
  function initTelemetry() {
    if (!el.telCpu) { return; }
    window.setInterval(paintUptime, 1000);
    window.setInterval(function () {
      if (!consoleVisible() || !serverUp()) { renderTelemetry(null); return; }
      fetch("/api/telemetry", { credentials: "same-origin" })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(renderTelemetry, function () { renderTelemetry(null); });
    }, TELEMETRY_MS);
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
    button.textContent = "Back up";
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
    var names = known.map(function (world) { return world.name; });
    if (!chosen || names.indexOf(chosen) < 0) {
      // Nothing picked yet (or the picked world is gone): open on the world the server
      // loads, not on whichever name sorts first -- that one is what the note describes.
      var loaded = known.filter(function (world) { return world.active; })[0];
      chosen = loaded ? loaded.name : "";
    }
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
      // The worlds route answers with the worlds panel, which knows nothing about the
      // backups table directly below it -- so that table is re-read here instead of
      // sitting one archive out of date until someone presses Refresh.
      refreshBackups();
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

  // ---------------------------------------------------------------- players
  //
  // The three list files belong to the game server, and they are the only record of
  // who is an admin, banned or permitted: the manager never keeps a copy. Every answer
  // from /api/players carries the roster AND each file's contents, so the table and
  // the raw editors are always painted from one read and cannot disagree.
  //
  // Two rules run through everything below. A control must never show a state the
  // file does not have -- a refused change repaints from a fresh read. And the
  // permitted list is a whitelist: while it holds even one active line, only the
  // players on it can join. The manager enforces that for the table and add-by-ID
  // (a permitted add while the list is off is parked, never written as an active
  // line); the raw editor writes exactly what is typed, so it asks here first.

  var PLAYER_LISTS = ["admin", "banned", "permitted"];
  var PLAYER_LIST_LABELS = { admin: "Admin", banned: "Banned", permitted: "Permitted" };
  // How a parked entry is written. The manager's own marker, parsed back by
  // permission_lists._PARKED_RE; the game reads it as a comment and ignores it.
  var PARKED_PREFIX = "// disabled-by-manager ";

  var lastPlayers = null;
  var playersLoaded = false;
  var playersBusy = false;
  // What the whitelist dialog does if the operator says yes. Read on confirm, and
  // cleared by every way out of the dialog, so a stale question can never be answered.
  var pendingWhitelist = null;
  // Per raw editor: the text it was last seeded with, and whether the operator has
  // typed in it since. A refresh must never throw away what someone is typing.
  var rawSeeds = {};
  var rawDirty = {};

  // The server's wall clock for a stored instant: the same anchor and the same trick
  // as paintClock() and formatTaken(), so a roster time and the header clock agree
  // whatever zone the browser is in. `null` is a player the log has never shown.
  function formatSeen(epoch) {
    if (epoch === null || epoch === undefined) { return "never"; }
    return formatTaken(epoch);
  }

  function rawEditor(kind) { return document.getElementById("raw-" + kind + "-text"); }

  // A list as the raw editor shows it: parked entries as the marker lines the manager
  // writes, then the active ids. Lossless on purpose -- /api/players/raw writes the
  // parked entries it parses out of the submitted text, so a box seeded from the ids
  // alone would, while the permitted list is off, save as an empty list and erase
  // everyone on it. The game's own header note is not shown; the manager keeps it.
  function rawText(list) {
    var lines = [];
    var i;
    for (i = 0; i < list.parked.length; i++) { lines.push(PARKED_PREFIX + list.parked[i]); }
    for (i = 0; i < list.ids.length; i++) { lines.push(list.ids[i]); }
    return lines.length ? lines.join("\n") + "\n" : "";
  }

  // The lines of raw text the game would act on: anything not blank and not a
  // comment. The same split permission_lists.parse_list_text makes.
  function activeLines(text) {
    var out = [];
    var lines = String(text || "").split(/\r?\n/);
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].trim();
      if (line && line.indexOf("//") !== 0) { out.push(line); }
    }
    return out;
  }

  function normaliseList(list) {
    var source = list || {};
    return {
      ids: Array.isArray(source.ids) ? source.ids.slice() : [],
      parked: Array.isArray(source.parked) ? source.parked.slice() : []
    };
  }

  // Whether a row's box is ticked. The manager's is_permitted already counts a parked
  // entry as on the list -- ready, not in force -- so every column reads the same way.
  function onList(kind, row) {
    return !!row["is_" + kind];
  }

  function showPlayersError(text) {
    if (!el.playersError) { return; }
    el.playersError.textContent = text || "";
    el.playersError.hidden = !text;
    markTab("players", text ? "error" : "", "error");
  }

  function renderPlayers(payload) {
    if (!el.playersBody || !payload) { return; }
    playersLoaded = true;
    var lists = payload.lists || {};
    lastPlayers = {
      players: Array.isArray(payload.players) ? payload.players : [],
      lists: {
        admin: normaliseList(lists.admin),
        banned: normaliseList(lists.banned),
        permitted: normaliseList(lists.permitted)
      },
      whitelistEnabled: payload.whitelist_enabled === true,
      conflicts: Array.isArray(payload.list_env_conflicts) ? payload.list_env_conflicts : []
    };

    el.playersBody.textContent = "";
    var shown = 0;
    for (var i = 0; i < lastPlayers.players.length; i++) {
      if (!inFilter(lastPlayers.players[i])) { continue; }
      el.playersBody.appendChild(playerRow(lastPlayers.players[i], i));
      shown++;
    }
    var any = !!lastPlayers.players.length;
    el.playersTable.hidden = !shown;
    el.playersEmpty.hidden = any;
    el.playersFilterEmpty.hidden = !any || !!shown;
    el.playersFilterEmptyText.textContent = playerFilter === "online"
      ? "No vikings currently active" : "Nobody here yet";
    renderFilters(lastPlayers.players);

    el.whitelistToggle.checked = lastPlayers.whitelistEnabled;
    // Said only while it is on: that is the state that turns people away.
    el.whitelistNote.textContent = lastPlayers.whitelistEnabled
      ? "Whitelist is on: only Permitted players can join." : "";
    el.whitelistNote.hidden = !lastPlayers.whitelistEnabled;
    // Beside the column itself, because that is where the worry arises: a tick there
    // looks like it might shut people out, and while the list is off it cannot.
    el.permittedNote.hidden = lastPlayers.whitelistEnabled;

    renderEnvConflicts(lastPlayers.conflicts);
    for (var k = 0; k < PLAYER_LISTS.length; k++) { seedRawEditor(PLAYER_LISTS[k]); }
    syncPlayerControls();
  }

  function renderEnvConflicts(names) {
    if (!el.playersEnvConflict) { return; }
    if (!names.length) {
      el.playersEnvConflict.hidden = true;
      el.playersEnvConflict.textContent = "";
      return;
    }
    var one = names.length === 1;
    el.playersEnvConflict.textContent =
      names.join(" and ") + (one ? " is" : " are") + " set in valheim.env. The game " +
      "server rewrites the matching list from " + (one ? "it" : "them") + " every time " +
      "it starts, so anything you change here will be thrown away at the next start. " +
      "Delete " + (one ? "that line" : "those lines") + " from valheim.env (or leave " +
      (one ? "it" : "them") + " empty) to manage the lists from this page.";
    el.playersEnvConflict.hidden = false;
  }

  function seedRawEditor(kind) {
    var box = rawEditor(kind);
    if (!box) { return; }
    var text = rawText(lastPlayers.lists[kind]);
    var stale = document.getElementById("raw-" + kind + "-stale");
    if (!rawDirty[kind]) {
      box.value = text;
      rawSeeds[kind] = text;
      if (stale) { stale.hidden = true; }
      return;
    }
    // Being typed in: leave it, but say so if the file moved underneath.
    if (stale) { stale.hidden = text === rawSeeds[kind]; }
  }

  var COPY_ICON =
    '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="5" y="5" width="9" height="9" ' +
    'rx="1.5" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M3 11V3a1 1 0 0 1 ' +
    '1-1h8" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>';

  function playerRow(row, index) {
    var tr = document.createElement("tr");
    if (row.online) { tr.className = "is-online"; }
    var who = document.createElement("td");
    who.className = "player-name";
    // The name only when the log let the watcher attach one with certainty; the ID
    // alone is the honest fallback, never a guess at whose name it is.
    var title = document.createElement("div");
    title.className = "player-title";
    title.textContent = row.name || row.id;
    if (row.first_seen) { title.title = "First joined " + formatSeen(row.first_seen); }
    who.appendChild(title);
    var idLine = document.createElement("div");
    idLine.className = "player-idline";
    if (row.name) {
      var id = document.createElement("span");
      id.className = "player-id";
      id.textContent = row.id;
      idLine.appendChild(id);
    }
    var copy = document.createElement("button");
    copy.type = "button";
    copy.className = "copy-id";
    copy.setAttribute("data-copy-id", row.id);
    copy.setAttribute("aria-label", "Copy ID " + row.id);
    copy.title = "Copy ID";
    copy.innerHTML = COPY_ICON;  // a fixed icon, no data in it
    if (row.name) {
      idLine.appendChild(copy);
      who.appendChild(idLine);
    } else {
      // No name: the title IS the ID, so the copy button sits beside it.
      title.appendChild(copy);
    }
    var noteId = "";
    if (!row.file_id) {
      // The log's ID could not be turned into the form the lists use without guessing,
      // and a guessed line is one the game silently ignores while the page says it
      // worked. So this row cannot be ticked, and says what to do instead.
      noteId = "player-needs-id-" + index;
      var note = document.createElement("div");
      note.className = "player-note";
      note.id = noteId;
      note.textContent = "ID unknown: add them by their F2 ID above.";
      who.appendChild(note);
    }
    tr.appendChild(who);

    tr.appendChild(statusCell(row));
    tr.appendChild(textCell(formatSeen(row.last_seen), "player-seen"));
    tr.appendChild(textCell(row.last_world || "—", "player-world"));

    var roles = document.createElement("td");
    roles.className = "col-roles";
    var group = document.createElement("div");
    group.className = "roles";
    for (var k = 0; k < PLAYER_LISTS.length; k++) {
      group.appendChild(rolePill(PLAYER_LISTS[k], row, noteId));
    }
    roles.appendChild(group);
    tr.appendChild(roles);
    return tr;
  }

  function statusCell(row) {
    var td = document.createElement("td");
    var status = document.createElement("span");
    var dot = document.createElement("span");
    var label = document.createElement("span");
    if (row.online) {
      status.className = "player-status is-online";
      dot.className = "dot dot-online";
      label.textContent = "Online";
      status.appendChild(dot);
      status.appendChild(label);
      var length = onlineFor(row.online_since);
      if (length) {
        var forText = document.createElement("span");
        forText.className = "for";
        forText.textContent = length.replace(/^for /, "");
        status.appendChild(forText);
      }
      status.title = "Online since " + formatSeen(row.online_since);
    } else if (row.seen) {
      status.className = "player-status is-offline";
      dot.className = "dot dot-idle";
      label.textContent = "Offline";
      status.appendChild(dot);
      status.appendChild(label);
    } else {
      status.className = "player-status is-never";
      label.textContent = "Never joined";
      status.appendChild(label);
    }
    td.appendChild(status);
    return td;
  }

  function textCell(text, cls) {
    var td = document.createElement("td");
    if (cls) { td.className = cls; }
    td.textContent = text;
    return td;
  }

  // A role pill: a real checkbox underneath (same events, same .checked, same safety
  // rules), drawn as a coloured chip. Admin gold, Banned crimson, Permitted emerald.
  function rolePill(kind, row, noteId) {
    var label = document.createElement("label");
    label.className = "role role-" + kind;
    var box = document.createElement("input");
    box.type = "checkbox";
    box.checked = onList(kind, row);
    box.setAttribute("data-player-list", kind);
    box.setAttribute("data-file-id", row.file_id || "");
    box.setAttribute("aria-label", PLAYER_LIST_LABELS[kind] + ": " + (row.name || row.id));
    if (!row.file_id) {
      box.disabled = true;
      box.setAttribute("data-needs-id", "true");
      box.setAttribute("aria-describedby", noteId);
    }
    if (kind === "permitted" && lastPlayers && !lastPlayers.whitelistEnabled && box.checked) {
      label.className += " is-parked";
      label.title = "On the permitted list; the whitelist is off.";
    }
    var pill = document.createElement("span");
    pill.className = "role-pill";
    pill.textContent = PLAYER_LIST_LABELS[kind];
    label.appendChild(box);
    label.appendChild(pill);
    return label;
  }

  // ------------------------------------------------------------- filters

  var playerFilter = "all";
  var FILTER_TESTS = {
    all: function () { return true; },
    online: function (row) { return !!row.online; },
    admin: function (row) { return !!row.is_admin; },
    banned: function (row) { return !!row.is_banned; },
    permitted: function (row) { return !!row.is_permitted; }
  };

  function inFilter(row) {
    return (FILTER_TESTS[playerFilter] || FILTER_TESTS.all)(row);
  }

  function renderFilters(players) {
    if (!el.playersFilters) { return; }
    var buttons = el.playersFilters.querySelectorAll("[data-filter]");
    for (var i = 0; i < buttons.length; i++) {
      var name = buttons[i].getAttribute("data-filter");
      var test = FILTER_TESTS[name] || FILTER_TESTS.all;
      var count = 0;
      for (var j = 0; j < players.length; j++) { if (test(players[j])) { count++; } }
      var badge = buttons[i].querySelector("[data-count]");
      if (badge) { badge.textContent = String(count); }
      buttons[i].setAttribute("aria-pressed", name === playerFilter ? "true" : "false");
    }
  }

  function setPlayerFilter(name) {
    if (!FILTER_TESTS[name]) { return; }
    playerFilter = name;
    if (lastPlayers) { renderPlayers(lastPlayersPayload()); }
  }

  // Copies an ID; says so on the button for a moment. Falls back to a hidden textarea
  // where the async clipboard API is missing (plain http on a LAN address).
  function copyText(text, button) {
    function done() {
      if (!button) { return; }
      var before = button.title;
      button.classList.add("is-copied");
      button.title = "Copied";
      window.setTimeout(function () { button.classList.remove("is-copied"); button.title = before; }, 1200);
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text); done(); });
      return;
    }
    fallbackCopy(text);
    done();
  }

  function fallbackCopy(text) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    try { document.execCommand("copy"); } catch (err) { /* nothing more to try */ }
    document.body.removeChild(area);
  }

  function openRawLists() {
    if (el.rawDialog && typeof el.rawDialog.showModal === "function") {
      el.rawDialog.showModal();
    } else if (el.rawDialog) {
      el.rawDialog.setAttribute("open", "");
    }
  }

  function closeRawLists() {
    if (!el.rawDialog) { return; }
    if (el.rawDialog.close && el.rawDialog.open) { el.rawDialog.close(); }
    else { el.rawDialog.removeAttribute("open"); }
  }

  function syncPlayerControls() {
    if (!el.playersBody) { return; }
    var busy = playersBusy;
    var boxes = el.playersBody.querySelectorAll("input[data-player-list]");
    for (var i = 0; i < boxes.length; i++) {
      // A row without a usable ID stays disabled whatever else is going on.
      boxes[i].disabled = busy || boxes[i].getAttribute("data-needs-id") === "true";
    }
    el.whitelistToggle.disabled = busy;
    el.playersRefresh.disabled = busy;
    el.playerAddKind.disabled = busy;
    el.playerAddId.disabled = busy;
    el.playerAddButton.disabled = busy;
    var buttons = document.querySelectorAll("[data-raw-save], [data-raw-reset]");
    for (var b = 0; b < buttons.length; b++) { buttons[b].disabled = busy; }
  }

  function refusal(payload) {
    var err = new Error((payload && payload.error) || "The manager refused that without saying why.");
    err.fromManager = true;
    return err;
  }

  function errorText(err) {
    return err && err.fromManager
      ? err.message
      : "Lost contact with the manager: " + ((err && err.message) || err);
  }

  // One request to the players routes, as a promise of the payload. A refusal is
  // thrown carrying the manager's own words, which already say what to do.
  function playersRequest(url, body) {
    var init = { credentials: "same-origin" };
    if (body !== undefined) {
      init.method = "POST";
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(body);
    }
    return fetch(url, init).then(function (response) {
      if (response.status === 401) { window.location.href = "/login"; return null; }
      return response.json().catch(function () { return {}; }).then(function (payload) {
        if (!response.ok) { throw refusal(payload); }
        return payload;
      });
    });
  }

  // ------------------------------------------------------------ online now

  var ONLINE_POLL_MS = 5000;
  var onlinePoll = null;
  // The player the ban dialog is asking about, by file ID. Read on confirm and cleared
  // by every way out, so a refresh underneath an open dialog cannot retarget it.
  var pendingBan = null;

  function startOnlinePoll() {
    if (onlinePoll !== null) { return; }
    onlinePoll = window.setInterval(function () {
      // Never underneath a change in flight or an open question: the repaint would
      // only race the answer.
      var asking = pendingBan !== null || pendingWhitelist !== null;
      if (!playersBusy && !asking) { refreshPlayers(); }
    }, ONLINE_POLL_MS);
  }

  function stopOnlinePoll() {
    if (onlinePoll === null) { return; }
    window.clearInterval(onlinePoll);
    onlinePoll = null;
  }

  // "for 23 min", on the server's clock so it agrees with the header. Empty until the
  // clock has its first anchor, rather than a number measured on the browser's.
  function onlineFor(since) {
    if (clock.epoch === null || since === null || since === undefined) { return ""; }
    var now = clock.epoch + (Date.now() - clock.takenAt) / 1000;
    var minutes = Math.max(0, Math.floor((now - Number(since)) / 60));
    if (minutes < 60) { return "for " + minutes + " min"; }
    return "for " + Math.floor(minutes / 60) + " h " + (minutes % 60) + " min";
  }

  function onlinePlayer(fileId) {
    if (!lastPlayers) { return null; }
    for (var i = 0; i < lastPlayers.players.length; i++) {
      if (lastPlayers.players[i].file_id === fileId) { return lastPlayers.players[i]; }
    }
    return null;
  }

  function askBan(fileId) {
    var row = onlinePlayer(fileId);
    if (!row || !row.file_id) { return; }
    var label = row.name ? row.name + " (" + row.id + ")" : row.id;
    pendingBan = row.file_id;
    if (el.banDialog && typeof el.banDialog.showModal === "function") {
      el.banName.textContent = label;
      el.banDetail.textContent = "online since " + formatSeen(row.online_since);
      el.banDialog.showModal();
    } else if (window.confirm(
        "Ban " + label + "? They will not be able to join again until you untick them " +
        "under Banned. Whether a ban also removes someone who is playing right now has " +
        "not been tested yet.")) {
      // No <dialog> support: still ask, never ban straight off a click.
      confirmBan();
    } else {
      pendingBan = null;
    }
  }

  function confirmBan() {
    var fileId = pendingBan;
    closeBan();
    if (fileId) {
      sendPlayers("/api/players/list", { kind: "banned", file_id: fileId, member: true });
    }
  }

  function closeBan() {
    pendingBan = null;
    if (el.banDialog && el.banDialog.close && el.banDialog.open) { el.banDialog.close(); }
  }

  function refreshPlayers() {
    if (!el.playersBody) { return Promise.resolve(null); }
    return playersRequest("/api/players").then(function (payload) {
      if (payload) { renderPlayers(payload); }
      return payload;
    }).catch(function (err) {
      showPlayersError(errorText(err));
      return null;
    });
  }

  // lastPlayers back in the shape the manager sends, for repainting without a request.
  function lastPlayersPayload() {
    return {
      players: lastPlayers.players,
      lists: lastPlayers.lists,
      whitelist_enabled: lastPlayers.whitelistEnabled,
      list_env_conflicts: lastPlayers.conflicts
    };
  }

  // Put the controls back to the last state the files were known to be in.
  function repaintLastKnown() {
    if (lastPlayers) { renderPlayers(lastPlayersPayload()); } else { syncPlayerControls(); }
  }

  // Every change goes through here. Success repaints from the answer, which is the
  // files as they now stand. Failure shows the manager's words as they are, puts the
  // controls back to the last known state, and then reads the files again -- so a
  // refused tick never stays ticked.
  function sendPlayers(url, body, onSaved) {
    playersBusy = true;
    syncPlayerControls();
    return playersRequest(url, body).then(function (payload) {
      playersBusy = false;
      if (!payload) { syncPlayerControls(); return null; }
      if (onSaved) { onSaved(payload); }
      renderPlayers(payload);
      return payload;
    }).catch(function (err) {
      playersBusy = false;
      showPlayersError(errorText(err));
      repaintLastKnown();
      return refreshPlayers().then(function () { return null; });
    });
  }

  function onPlayerToggle(event) {
    var box = event.target;
    if (!box || !box.getAttribute || !box.getAttribute("data-player-list")) { return; }
    var kind = box.getAttribute("data-player-list");
    var fileId = box.getAttribute("data-file-id");
    showPlayersError("");
    // Never send a missing or guessed ID. The box is disabled for such a row; this is
    // the belt to that brace.
    if (!fileId || box.getAttribute("data-needs-id") === "true" || !lastPlayers) {
      repaintLastKnown();
      return;
    }
    // Banning someone who is playing right now asks first; the box shows the file's
    // state until the answer comes back.
    var row = onlinePlayer(fileId);
    if (kind === "banned" && box.checked && row && row.online) {
      box.checked = false;
      askBan(fileId);
      return;
    }
    sendPlayers("/api/players/list", { kind: kind, file_id: fileId, member: box.checked === true });
  }

  // Who the permitted list would let in, by name where one is known.
  function describeIds(ids) {
    var names = {};
    var players = lastPlayers ? lastPlayers.players : [];
    for (var i = 0; i < players.length; i++) {
      if (players[i].file_id && players[i].name) { names[players[i].file_id] = players[i].name; }
    }
    return ids.map(function (id) { return { id: id, name: names[id] || "" }; });
  }

  function permittedEntries() {
    if (!lastPlayers) { return []; }
    var list = lastPlayers.lists.permitted;
    var seen = {};
    var ids = [];
    list.parked.concat(list.ids).forEach(function (id) {
      if (!seen[id]) { seen[id] = 1; ids.push(id); }
    });
    return describeIds(ids);
  }

  function whitelistQuestion(entries) {
    var who = entries.length
      ? entries.map(function (e) { return e.name ? e.name + " (" + e.id + ")" : e.id; }).join(", ")
      : "nobody yet";
    return "Turn the permitted list on?\n\n" +
      "Only the players on it will be able to join. Everyone else is turned away when " +
      "they try, even with the right join password.\n\n" +
      "On the list: " + who + ".\n\n" +
      "The manager cannot tell which of these players is you, so it cannot keep you " +
      "on the list for you. Check you are on it before you go on.";
  }

  // Asks before anything that would switch the permitted list on. The <dialog> where
  // there is one; otherwise still ask, and never act straight off the click.
  function askWhitelist(entries, onConfirm) {
    if (!el.whitelistDialog) { return; }
    pendingWhitelist = onConfirm;
    el.whitelistCount.textContent = entries.length
      ? (entries.length === 1 ? "One player is on it:" : entries.length + " players are on it:")
      : "Nobody is on it yet.";
    el.whitelistList.textContent = "";
    for (var i = 0; i < entries.length; i++) {
      var item = document.createElement("li");
      item.textContent = entries[i].name ? entries[i].name + " " : "";
      var id = document.createElement("span");
      id.className = "player-id";
      id.textContent = entries[i].id;
      item.appendChild(id);
      el.whitelistList.appendChild(item);
    }
    el.whitelistList.hidden = !entries.length;
    if (typeof el.whitelistDialog.showModal === "function") {
      el.whitelistDialog.showModal();
    } else if (window.confirm(whitelistQuestion(entries))) {
      confirmWhitelist();
    } else {
      // Declined. Clearing this matters: a question left pending is one a later
      // confirm would answer without the operator ever having seen it.
      pendingWhitelist = null;
    }
  }

  function closeWhitelist() {
    pendingWhitelist = null;
    if (el.whitelistDialog && el.whitelistDialog.close && el.whitelistDialog.open) {
      el.whitelistDialog.close();
    }
  }

  function confirmWhitelist() {
    var run = pendingWhitelist;
    closeWhitelist();
    if (run) { run(); }
  }

  function saveRaw(kind) {
    var box = rawEditor(kind);
    if (!box) { return; }
    showPlayersError("");
    var text = box.value;
    var send = function () {
      sendPlayers("/api/players/raw", { kind: kind, text: text }, function () {
        // Saved: the box follows the file again, starting with this answer.
        rawDirty[kind] = false;
      });
    };
    // Active lines in a permitted list that is off would switch it on. That is the
    // same act as the switch above, so it gets the same question.
    var active = activeLines(text);
    if (kind === "permitted" && lastPlayers && !lastPlayers.whitelistEnabled && active.length) {
      askWhitelist(describeIds(active), send);
      return;
    }
    send();
  }

  function resetRaw(kind) {
    rawDirty[kind] = false;
    if (lastPlayers) { seedRawEditor(kind); }
  }

  function addPlayer() {
    showPlayersError("");
    sendPlayers("/api/players/add", { kind: el.playerAddKind.value, id: el.playerAddId.value }, function () {
      el.playerAddId.value = "";
    });
  }

  function initPlayers() {
    if (!el.playersBody) { return; }
    el.playersRefresh.addEventListener("click", function () {
      showPlayersError("");
      refreshPlayers();
    });
    el.playersBody.addEventListener("change", onPlayerToggle);

    el.whitelistToggle.addEventListener("change", function () {
      showPlayersError("");
      if (!el.whitelistToggle.checked) {
        // Off lets people in rather than keeping them out, so it needs no question.
        sendPlayers("/api/players/whitelist", { enabled: false });
        return;
      }
      // Not on until the operator says so: the box shows what the file says while the
      // question is open, and a Cancel leaves it exactly there.
      el.whitelistToggle.checked = false;
      askWhitelist(permittedEntries(), function () {
        sendPlayers("/api/players/whitelist", { enabled: true });
      });
    });
    el.whitelistConfirm.addEventListener("click", confirmWhitelist);
    el.whitelistCancel.addEventListener("click", closeWhitelist);
    // Esc closes a <dialog> on its own; make that forget the question too.
    el.whitelistDialog.addEventListener("close", function () { pendingWhitelist = null; });

    el.playersFilters.addEventListener("click", function (event) {
      var chip = event.target.closest ? event.target.closest("[data-filter]") : null;
      if (chip) { setPlayerFilter(chip.getAttribute("data-filter")); }
    });
    el.playersBody.addEventListener("click", function (event) {
      var copy = event.target.closest ? event.target.closest("[data-copy-id]") : null;
      if (copy) { copyText(copy.getAttribute("data-copy-id"), copy); }
    });
    el.playersRawButton.addEventListener("click", openRawLists);
    el.rawClose.addEventListener("click", closeRawLists);
    el.banConfirm.addEventListener("click", confirmBan);
    el.banCancel.addEventListener("click", closeBan);
    el.banDialog.addEventListener("close", function () { pendingBan = null; });

    el.playerAddForm.addEventListener("submit", function (event) {
      event.preventDefault();
      if (!el.playerAddButton.disabled) { addPlayer(); }
    });

    PLAYER_LISTS.forEach(function (kind) {
      var box = rawEditor(kind);
      if (box) { box.addEventListener("input", function () { rawDirty[kind] = true; }); }
    });
    // The raw editors sit in a modal now, so delegate from the whole panel.
    el.playersPanel.addEventListener("click", function (event) {
      var target = event.target && event.target.closest ? event.target : null;
      if (!target) { return; }
      var save = target.closest("[data-raw-save]");
      if (save && !save.disabled) { saveRaw(save.getAttribute("data-raw-save")); return; }
      var reset = target.closest("[data-raw-reset]");
      if (reset && !reset.disabled) { resetRaw(reset.getAttribute("data-raw-reset")); }
    });

    refreshPlayers();
  }

  // -------------------------------------------------------------- websocket

  function wsUrl() {
    var scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    return scheme + "//" + window.location.host + "/ws/logs";
  }

  var linkState = "connecting";

  function setLink(state) {
    linkState = state;
    paintBadge();
  }

  // The one status badge. Without a live connection the server's phase is unknown,
  // so the badge says that instead of repeating the last phase it heard.
  function paintBadge() {
    if (!el.badge) { return; }
    if (linkState !== "live" || !lastStatus) {
      var down = linkState === "down";
      el.badge.textContent = down ? "disconnected" : "connecting…";
      el.badge.className = "badge " + (down ? "badge-error" : "badge-busy");
      el.badge.title = down ? "Lost contact with the manager; retrying." : "";
      return;
    }
    var phase = PHASES[lastStatus.phase] || PHASES.error;
    el.badge.textContent = phase.label;
    el.badge.className = "badge " + phase.cls;
    el.badge.title = lastStatus.message || "";
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
        anchorClock(msg.server_clock);
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
  initBackups();
  initPlayers();
  startClock();

  el.start.addEventListener("click", function () { system("start requested"); post("/api/start"); });
  el.stop.addEventListener("click", function () { system("stop requested"); post("/api/stop"); });
  el.restart.addEventListener("click", function () { system("restart requested"); post("/api/restart"); });
  el.force.addEventListener("click", function () {
    system("force stop requested");
    forceOffered = false;
    el.force.hidden = true;
    post("/api/stop", { force: true });
  });
  // Links between panels ("on the Worlds tab"). Delegated, because the settings cards
  // that carry one are rebuilt from status pushes.
  document.addEventListener("click", function (event) {
    var jump = event.target.closest ? event.target.closest("[data-goto-tab]") : null;
    if (jump) { selectTab(jump.getAttribute("data-goto-tab"), true); }
  });
  el.clear.addEventListener("click", function () { el.console.textContent = ""; });
  el.consoleFilter.addEventListener("input", applyConsoleFilter);
  el.consoleCopy.addEventListener("click", copyConsole);
  initTelemetry();

  if (el.settingsForm && el.settingsEdit) {
    MASK = el.settingsForm.getAttribute("data-mask") || MASK;
    if (el.modifierFields) {
      // One listener on the fieldset: change events from every select and checkbox in
      // it bubble here, so adding a modifier to the markup needs no change in this file.
      el.modifierFields.addEventListener("change", renderModifierPreview);
      renderModifierPreview();
    }
    el.settingsEdit.addEventListener("click", openEditor);
    if (el.passPeek) { el.passPeek.addEventListener("click", togglePassPeek); }
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
