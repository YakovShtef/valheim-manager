// Drives the dashboard's tab strip in a real DOM.
//
// Everything here is behaviour the Python suite cannot reach: it renders markup and
// reads files as text, and never executes app.js. The tab strip is almost entirely
// runtime -- which panel is showing, whether the console kept its buffer, whether a
// lock reached the strip -- so without this file those promises are pinned by nothing.
//
//   node tabs.mjs <rendered-index.html> <app.js> <style.css>
//
// prints one JSON object of {case: {ok, detail}} on stdout. test_tabs_runtime.py
// renders the page through the app, runs this, and turns each case into a test.
//
// What jsdom CANNOT do: layout. Every scroll metric is whatever we stub it to be, so
// "the view is parked 1115px above the tail" is not reachable here and is checked by
// hand in a browser. What is reachable, and is what the fix actually promises, is
// whether the code re-follows the tail on the way back into the console -- so the
// scroll accessors below are instrumented and the assertions are about the writes.

import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const [htmlPath, appPath, cssPath] = process.argv.slice(2);
const HTML = readFileSync(htmlPath, "utf8");
const APP = readFileSync(appPath, "utf8");
const CSS = readFileSync(cssPath, "utf8");

const PANELS = ["panel-console", "panel-settings", "panel-worlds"];
const SCROLL_HEIGHT = 9999;
const CLIENT_HEIGHT = 300;

// The shape the manager actually pushes (settings_store.display_settings).
const SETTINGS_ROWS = [
  { key: "SERVER_NAME", label: "Server name", value: "My Server", secret: false },
  { key: "SERVER_PASS", label: "Join password", value: "********", secret: true },
  { key: "SERVER_PORT", label: "Game port", value: "2456", secret: false },
  // A key the manager has no plain name for: it arrives with its own name as label.
  { key: "MY_OWN_KEY", label: "MY_OWN_KEY", value: "42", secret: false },
];

function makePage({ stored = null, breakStorage = false, html = HTML } = {}) {
  const dom = new JSDOM(html, { url: "http://localhost/", runScripts: "outside-only" });
  const win = dom.window;
  const doc = win.document;

  // The real stylesheet, so `hidden` is judged against the real cascade rather than
  // against the attribute alone -- an author `display` beating `[hidden]` is the bug
  // that shipped in this project once already.
  const style = doc.createElement("style");
  style.textContent = CSS;
  doc.head.appendChild(style);

  if (breakStorage) {
    // Private mode: the accessor itself throws, on read as well as on write.
    Object.defineProperty(win, "localStorage", {
      configurable: true,
      get() { throw new Error("storage is blocked"); },
    });
  } else {
    win.localStorage.clear();
    if (stored !== null) { win.localStorage.setItem("valheim-manager.tab", stored); }
  }

  // Scroll instrumentation: jsdom has no layout, so these stand in for it and record
  // what the code tried to do.
  const scrollWrites = [];
  const pre = doc.getElementById("console");
  if (pre) {
    let top = 0;
    Object.defineProperty(pre, "scrollHeight", { configurable: true, get: () => SCROLL_HEIGHT });
    Object.defineProperty(pre, "clientHeight", { configurable: true, get: () => CLIENT_HEIGHT });
    Object.defineProperty(pre, "scrollTop", {
      configurable: true,
      get: () => top,
      set: (value) => { top = value; scrollWrites.push(value); },
    });
  }

  const fetches = [];
  let worldsPayload = { worlds: [], worlds_error: null, current: null };
  win.fetch = function (path) {
    fetches.push(String(path));
    return Promise.resolve({
      ok: true, status: 200, json: () => Promise.resolve(worldsPayload),
    });
  };

  const xhrs = [];
  win.XMLHttpRequest = function () {
    const self = this;
    self.upload = {};
    self.status = 0;
    self.aborted = false;
    self.open = function (method, url) { self.method = method; self.url = url; };
    self.setRequestHeader = function () {};
    self.send = function (body) { self.body = body; };   // hangs: that is the point
    self.abort = function () { self.aborted = true; if (self.onabort) { self.onabort(); } };
    xhrs.push(self);
  };

  const sockets = [];
  win.WebSocket = function (url) {
    const self = this;
    self.url = url;
    self.readyState = 1;
    self.closed = false;
    self.close = function () { self.closed = true; self.readyState = 3; };
    self.send = function () {};
    sockets.push(self);
    win.setTimeout(() => { if (self.onopen) { self.onopen(); } }, 0);
  };

  win.eval(APP);

  const push = (frame) => sockets[sockets.length - 1].onmessage({ data: JSON.stringify(frame) });
  const status = (phase) => push({
    type: "status",
    status: {
      phase,
      message: phase,
      container_state: phase === "stopped" ? "exited" : "running",
      container_exists: phase !== "absent",
      started_at: null,
      image: "ghcr.io/example/valheim-server:latest",
    },
    settings: SETTINGS_ROWS,
    settings_error: null,
    modifiers: {},
  });

  return {
    win, doc, fetches, xhrs, sockets, scrollWrites, push, status,
    setWorlds: (payload) => { worldsPayload = payload; },
    // A panel id that is not on the page counts as not shown rather than throwing:
    // one case deliberately renders a tab pointing at a panel that does not exist.
    shown: () => PANELS.filter((id) => {
      const panel = doc.getElementById(id);
      return panel && win.getComputedStyle(panel).display !== "none";
    }),
    selected: () => [...doc.querySelectorAll("[role=tab]")]
      .filter((b) => b.getAttribute("aria-selected") === "true").map((b) => b.dataset.tab),
    stops: () => [...doc.querySelectorAll("[role=tab]")].filter((b) => b.tabIndex === 0).map((b) => b.dataset.tab),
    tab: (name) => doc.querySelector(`[data-tab="${name}"]`),
    note: (name) => doc.querySelector(`[data-tab="${name}"] [data-tab-note]`),
    key: (k) => {
      const ev = new win.KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true });
      doc.activeElement.dispatchEvent(ev);
      return ev;
    },
    drop: (files) => {
      const ev = new win.Event("drop", { bubbles: true, cancelable: true });
      Object.defineProperty(ev, "dataTransfer", { value: { items: [], files } });
      doc.getElementById("world-dropzone").dispatchEvent(ev);
    },
    file: (name) => new win.File(["x"], name, { type: "application/octet-stream" }),
    settle: () => new Promise((resolve) => win.setTimeout(resolve, 0)),
  };
}

// ---------------------------------------------------------------------- cases

const cases = {};
const check = (name, fn) => { cases[name] = fn; };
const eq = (got, want, what) => {
  const a = JSON.stringify(got), b = JSON.stringify(want);
  if (a !== b) { throw new Error(`${what}: got ${a}, wanted ${b}`); }
};

check("lands_on_console_with_no_stored_tab", () => {
  const p = makePage();
  eq(p.selected(), ["console"], "selected tab");
  eq(p.shown(), ["panel-console"], "visible panels");
  eq(p.stops(), ["console"], "tab stops");
});

check("the_console_is_never_rebuilt_across_a_switch", () => {
  const p = makePage();
  const pre = p.doc.getElementById("console");
  const follow = p.doc.getElementById("follow");
  p.push({ type: "log", lines: Array.from({ length: 40 }, (_, i) => "line-" + i) });
  follow.checked = false;
  const before = pre.textContent;
  eq(before.includes("line-0"), true, "precondition: the first line is in the buffer");

  p.tab("worlds").click();
  eq(p.doc.body.contains(pre), true, "the <pre> left the DOM while hidden");
  p.push({ type: "log", lines: ["line-while-hidden"] });
  p.tab("settings").click();
  p.tab("console").click();

  const after = p.doc.getElementById("console");
  eq(after === pre, true, "the console element was replaced");
  eq(after.textContent.startsWith(before), true, "earlier lines were lost");
  eq(after.textContent.includes("line-while-hidden"), true, "a line that arrived while hidden was lost");
  eq(p.doc.getElementById("follow").checked, false, "the follow checkbox was reset");
});

check("returning_to_the_console_resumes_following", () => {
  // The bug this pins: while the panel is display:none every scroll metric reads 0,
  // so append()'s follow-scroll is a no-op for every line that arrives meanwhile and
  // the browser restores the OLD offset on the way back. Following has to be
  // re-established when the panel is shown, or "follow" silently stops following.
  const p = makePage();
  p.doc.getElementById("follow").checked = true;
  p.tab("worlds").click();
  p.push({ type: "log", lines: ["while you were away"] });
  const before = p.scrollWrites.length;
  p.tab("console").click();
  const written = p.scrollWrites.slice(before);
  eq(written.length > 0, true, "showing the console did not re-follow the tail");
  eq(written[written.length - 1], SCROLL_HEIGHT, "re-followed to the wrong offset");
});

check("returning_to_the_console_leaves_a_parked_view_alone", () => {
  // The other half: with follow OFF the operator is reading something, and a tab
  // switch must not yank them to the bottom.
  const p = makePage();
  p.doc.getElementById("follow").checked = false;
  p.doc.getElementById("console").scrollTop = 1200;
  p.tab("settings").click();
  const before = p.scrollWrites.length;
  p.tab("console").click();
  eq(p.scrollWrites.slice(before), [], "a parked console was scrolled by the tab switch");
  eq(p.doc.getElementById("console").scrollTop, 1200, "the parked position moved");
});

check("a_switch_issues_no_request_and_leaves_the_socket_alone", () => {
  const p = makePage();
  p.status("stopped");
  const sock = p.sockets[p.sockets.length - 1];
  const fetchesBefore = p.fetches.length;
  const xhrsBefore = p.xhrs.length;

  p.tab("worlds").click();
  p.tab("settings").click();
  p.tab("console").click();

  eq(p.fetches.slice(fetchesBefore), [], "a tab switch fetched");
  eq(p.xhrs.length, xhrsBefore, "a tab switch opened an XHR");
  eq(p.sockets.length, 1, "a tab switch opened another socket");
  eq(p.sockets[0] === sock, true, "the socket was replaced");
  eq(sock.closed, false, "the socket was closed by a tab switch");
});

check("running_leaves_both_tabs_openable_but_locked", () => {
  const p = makePage();
  p.status("ready");
  const dis = (id) => p.doc.getElementById(id).disabled;

  // Openable: the tab buttons are never disabled, and they still work.
  eq(p.tab("settings").disabled, false, "the settings tab button is disabled");
  eq(p.tab("worlds").disabled, false, "the worlds tab button is disabled");
  p.tab("settings").click();
  eq(p.shown(), ["panel-settings"], "the settings panel did not open while running");

  // ...the current values are readable...
  eq(p.doc.getElementById("settings-table").hidden, false, "the settings table is hidden while running");
  eq(p.doc.querySelectorAll("#settings-table tbody tr").length, SETTINGS_ROWS.length, "settings rows to read");

  // ...but editing is refused, with the reason on screen...
  eq(dis("btn-settings-edit"), true, "Edit is offered while running");
  eq(p.doc.getElementById("settings-locked").hidden, false, "no lock reason shown for settings");
  eq(p.doc.getElementById("settings-locked").textContent.length > 0, true, "the settings lock reason is empty");
  eq(dis("btn-world-upload"), true, "Upload is offered while running");
  eq(dis("btn-pick-folder"), true, "the folder picker is offered while running");
  eq(p.doc.getElementById("worlds-locked").hidden, false, "no lock reason shown for worlds");

  // ...and the strip says so, so a hidden panel's state is not a surprise.
  eq(p.note("settings").hidden, false, "the settings tab carries no locked mark");
  eq(p.note("settings").textContent, "locked", "the settings tab's mark");
  eq(p.note("worlds").textContent, "locked", "the worlds tab's mark");
});

check("stopping_unlocks_both_tabs_without_a_tab_switch", () => {
  const p = makePage();
  p.status("ready");
  eq(p.note("settings").hidden, false, "precondition: locked while running");
  p.status("stopped");
  eq(p.doc.getElementById("btn-settings-edit").disabled, false, "Edit stayed disabled after stopping");
  eq(p.doc.getElementById("btn-pick-folder").disabled, false, "the picker stayed disabled after stopping");
  eq(p.doc.getElementById("settings-locked").hidden, true, "the lock reason stayed on screen");
  eq(p.note("settings").hidden, true, "the settings tab kept its locked mark");
  eq(p.note("worlds").hidden, true, "the worlds tab kept its locked mark");
});

check("a_stored_tab_comes_back_on_load", () => {
  const p = makePage({ stored: "worlds" });
  eq(p.selected(), ["worlds"], "selected tab");
  eq(p.shown(), ["panel-worlds"], "visible panels");
});

check("an_unusable_stored_tab_falls_back_to_console", () => {
  for (const junk of ["chat", "", "panel-console", "Settings", "worlds ", "__proto__"]) {
    const p = makePage({ stored: junk });
    eq(p.selected(), ["console"], `selected tab for stored ${JSON.stringify(junk)}`);
    eq(p.shown(), ["panel-console"], `visible panels for stored ${JSON.stringify(junk)}`);
  }
});

check("a_switch_is_remembered", () => {
  const p = makePage();
  p.tab("worlds").click();
  eq(p.win.localStorage.getItem("valheim-manager.tab"), "worlds", "stored tab after a switch");
});

check("blocked_storage_still_leaves_a_usable_dashboard", () => {
  const p = makePage({ breakStorage: true });
  eq(p.selected(), ["console"], "selected tab");
  eq(p.shown(), ["panel-console"], "visible panels");
  p.tab("worlds").click();
  eq(p.shown(), ["panel-worlds"], "switching broke with storage unavailable");
});

check("the_keyboard_walks_the_strip", () => {
  const p = makePage();
  p.tab("console").focus();
  const step = (k) => { const ev = p.key(k); return { tab: p.selected()[0], prevented: ev.defaultPrevented }; };

  eq(step("ArrowRight").tab, "settings", "ArrowRight from console");
  eq(step("ArrowRight").tab, "worlds", "ArrowRight from settings");
  eq(step("ArrowRight").tab, "console", "ArrowRight wraps past the last tab");
  eq(step("ArrowLeft").tab, "worlds", "ArrowLeft wraps past the first tab");
  eq(step("Home").tab, "console", "Home");
  eq(step("End").tab, "worlds", "End");
  eq(p.stops().length, 1, "more than one tab stop after moving");

  p.tab("settings").focus();
  const enter = p.key("Enter");
  eq(p.selected(), ["settings"], "Enter did not activate the focused tab");
  eq(enter.defaultPrevented, true, "Enter was not prevented");
  p.tab("worlds").focus();
  const space = p.key(" ");
  eq(p.selected(), ["worlds"], "Space did not activate the focused tab");
  eq(space.defaultPrevented, true, "Space was not prevented (it would scroll the page)");
});

check("exactly_one_panel_is_ever_visible", () => {
  const p = makePage();
  for (const name of ["settings", "worlds", "console", "worlds", "settings"]) {
    p.tab(name).click();
    eq(p.shown(), ["panel-" + name], `visible panels after selecting ${name}`);
  }
});

check("the_settings_table_shows_plain_names", () => {
  // The editor's labels and the read-only table used to disagree about what the same
  // row was called. The manager names each setting now; the table has to use it.
  const p = makePage();
  p.status("ready");
  const names = [...p.doc.querySelectorAll("#settings-table tbody tr th")].map((th) => th.textContent);
  eq(names, ["Server name", "Join password", "Game port", "MY_OWN_KEY"], "the row names");
});

check("every_panel_can_take_focus", () => {
  // While the server runs the settings panel holds nothing focusable -- Edit is
  // disabled and the form is hidden -- so without a tabindex of its own a keyboard
  // operator tabs straight out of the page instead of into the values the panel was
  // deliberately left open to show.
  const p = makePage();
  p.status("ready");
  for (const name of ["console", "settings", "worlds"]) {
    p.tab(name).click();
    const panel = p.doc.getElementById("panel-" + name);
    eq(panel.getAttribute("tabindex"), "0", `${name} panel is not focusable`);
    panel.focus();
    eq(p.doc.activeElement.id, "panel-" + name, `${name} panel could not take focus`);
  }
});

check("a_panel_error_survives_a_switch_and_is_marked_in_the_strip", async () => {
  const p = makePage();
  p.status("stopped");
  p.setWorlds({ worlds: [], worlds_error: "the world directory is unreadable", current: null });
  p.doc.getElementById("btn-worlds-refresh").click();
  await p.settle();

  const err = p.doc.getElementById("worlds-error");
  eq(err.hidden, false, "the worlds error was not shown");
  const text = err.textContent;
  eq(text.length > 0, true, "the worlds error is empty");
  eq(p.note("worlds").textContent, "error", "the worlds tab carries no error mark");

  p.tab("console").click();
  p.tab("worlds").click();
  eq(p.doc.getElementById("worlds-error").hidden, false, "the error was lost by switching tabs");
  eq(p.doc.getElementById("worlds-error").textContent, text, "the error text changed across a switch");
});

check("an_upload_in_flight_is_marked_in_the_strip", async () => {
  // The progress bar and its abort both live inside the worlds panel, so from any
  // other tab an upload in flight would otherwise be entirely invisible -- while
  // Start is still live and the manager would refuse it.
  const p = makePage();
  p.status("stopped");
  await p.settle();
  p.drop([p.file("MyWorld.db"), p.file("MyWorld.fwl")]);
  eq(p.doc.getElementById("btn-world-upload").disabled, false, "Upload was not offered for a valid drop");

  p.doc.getElementById("btn-world-upload").click();
  await p.settle();
  eq(p.xhrs.length, 1, "the upload did not start");
  eq(p.note("worlds").hidden, false, "an upload in flight leaves no mark in the strip");
  eq(p.note("worlds").textContent, "uploading", "the worlds tab's mark during an upload");

  // ...and it is still there from another tab, which is the whole point.
  p.tab("console").click();
  eq(p.note("worlds").textContent, "uploading", "the mark vanished when the panel was hidden");
});

check("a_tab_pointing_at_a_missing_panel_does_not_kill_the_dashboard", () => {
  // findTab guards the CHOSEN tab; selectTab used to dereference every OTHER tab's
  // panel without the same guard. Because initTabs runs at module scope, that throw
  // took the rest of the IIFE with it: no socket, no listeners, Start disabled for
  // good. One typo in an aria-controls is all it took.
  const broken = HTML.replace('id="panel-worlds"', 'id="panel-worlds-typo"');
  const p = makePage({ html: broken });
  eq(p.sockets.length, 1, "the socket never opened -- the IIFE died");
  eq(p.selected(), ["console"], "the dashboard did not land on the console");
  eq(p.shown(), ["panel-console"], "visible panels");
  p.tab("settings").click();
  eq(p.shown(), ["panel-settings"], "the surviving tabs stopped working");
});

// -------------------------------------------------------------------- runner

const results = {};
for (const [name, fn] of Object.entries(cases)) {
  try {
    await fn();
    results[name] = { ok: true, detail: "" };
  } catch (err) {
    results[name] = { ok: false, detail: String((err && err.message) || err) };
  }
}
process.stdout.write(JSON.stringify(results, null, 2));
