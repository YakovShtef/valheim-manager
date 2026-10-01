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

// Every page built, so they can be closed at the end. A JSDOM window holds node's
// event loop open for as long as anything in it has a live timer, and the dashboard
// has one by design: the header clock ticks every 250ms for the life of the page.
// Leaving ~20 windows open meant this process finished its work and then never
// exited, so pytest killed it at its timeout -- once per test, 25 times.
const PAGES = [];
const PANELS = ["panel-console", "panel-settings", "panel-worlds", "panel-mods", "panel-players"];
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

// The shape /api/players answers with (main.py _roster_payload). Three rows, one of
// each kind the table has to draw: a named player who has joined, a player whose log
// ID could not be converted to the file form (file_id null), and an ID that is only in
// a file and has never joined -- plus a parked-only ID, which the manager also gives a
// row. The permitted list is OFF: its entries are parked, and is_permitted counts them.
const RAGNAR = "V_76561198012345678";
const PLAYERS = (over = {}) => ({
  players: [
    { id: RAGNAR, platform_id: "76561198012345678", file_id: RAGNAR, name: "Ragnar",
      first_seen: 1699990000, last_seen: 1700000040, last_world: "Dedicated", seen: true,
      is_admin: true, is_banned: false, is_permitted: true },
    { id: "xbox-2535411", platform_id: "xbox-2535411", file_id: null, name: null,
      first_seen: 1699000000, last_seen: 1699500000, last_world: "Dedicated", seen: true,
      is_admin: false, is_banned: false, is_permitted: false },
    { id: "V_1111", platform_id: null, file_id: "V_1111", name: null,
      first_seen: null, last_seen: null, last_world: null, seen: false,
      is_admin: false, is_banned: true, is_permitted: false },
    { id: "V_2222", platform_id: null, file_id: "V_2222", name: null,
      first_seen: null, last_seen: null, last_world: null, seen: false,
      is_admin: false, is_banned: false, is_permitted: true },
  ],
  lists: {
    admin: { ids: [RAGNAR], comments: ["// List admin players ID  ONE per line"], parked: [] },
    banned: { ids: ["V_1111"], comments: ["// List banned players ID  ONE per line"], parked: [] },
    permitted: { ids: [], comments: ["// List permitted players ID ONE per line"],
                 parked: [RAGNAR, "V_2222"] },
  },
  whitelist_enabled: false,
  list_env_conflicts: [],
  ...over,
});

function makePage({ stored = null, breakStorage = false, html = HTML } = {}) {
  const dom = new JSDOM(html, { url: "http://localhost/", runScripts: "outside-only" });
  PAGES.push(dom);
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
  let worldsPayload = {
    worlds: [
      { name: "Dedicated", layout: "1.0", legacy: false, size: "48.2 MB", files: 812,
        active: true, loadable: true, unloadable: "" },
      { name: "Oldsave", layout: "legacy", legacy: true, size: "9.1 MB", files: 2,
        active: false, loadable: true, unloadable: "" },
    ],
    worlds_error: null,
    current: "Dedicated",
  };
  // The players routes answer with the roster; a case can swap in its own answer per
  // request (a refusal, a file that changed elsewhere) with `answerPlayers`.
  const posts = [];
  let playersPayload = PLAYERS();
  let playersAnswer = null;
  win.fetch = function (path, init) {
    const url = String(path);
    fetches.push(url);
    if (init && init.method === "POST") {
      let body = null;
      try { body = JSON.parse(init.body); } catch (err) { body = null; }  // FormData
      posts.push({ path: url, body });
    }
    if (url.indexOf("/api/players") === 0) {
      const custom = playersAnswer ? playersAnswer(url, init) : null;
      const answer = custom || { status: 200, payload: playersPayload };
      return Promise.resolve({
        ok: answer.status < 400, status: answer.status,
        json: () => Promise.resolve(answer.payload),
      });
    }
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
    win, doc, fetches, posts, xhrs, sockets, scrollWrites, push, status,
    setWorlds: (payload) => { worldsPayload = payload; },
    setPlayers: (payload) => { playersPayload = payload; },
    answerPlayers: (fn) => { playersAnswer = fn; },
    // Ancestor-aware on purpose. getComputedStyle reports the ELEMENT's own display,
    // so a panel nested inside another panel reads as "block" while being completely
    // invisible -- which is exactly how a fourth panel once shipped inside the third.
    // A panel id that is not on the page counts as not shown rather than throwing:
    // one case deliberately renders a tab pointing at a panel that does not exist.
    shown: () => PANELS.filter((id) => {
      let node = doc.getElementById(id);
      while (node && node !== doc.documentElement) {
        if (win.getComputedStyle(node).display === "none" || node.hidden) { return false; }
        node = node.parentElement;
      }
      return !!doc.getElementById(id);
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
    hover: (node, type) => node.dispatchEvent(
      new win.MouseEvent(type, { bubbles: true, cancelable: true })),
    bubbleOf: (node) => node.closest(".hint-anchor").querySelector(".hint-bubble"),
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
  eq(step("ArrowRight").tab, "mods", "ArrowRight from worlds");
  eq(step("ArrowRight").tab, "players", "ArrowRight from mods");
  eq(step("ArrowRight").tab, "console", "ArrowRight wraps past the last tab");
  eq(step("ArrowLeft").tab, "players", "ArrowLeft wraps past the first tab");
  eq(step("Home").tab, "console", "Home");
  eq(step("End").tab, "players", "End");
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
  for (const name of ["settings", "worlds", "mods", "players", "console", "players", "settings"]) {
    p.tab(name).click();
    eq(p.shown(), ["panel-" + name], `visible panels after selecting ${name}`);
  }
});

check("the_active_world_explains_its_refused_delete_on_hover", async () => {
  // "load another world first" sitting under the button said nothing on its own. The
  // reason belongs on the control it is about, at the moment you reach for it.
  const p = makePage();
  p.status("stopped");
  await p.settle();

  const refused = p.doc.querySelector('button[data-delete-world="Dedicated"]');
  eq(!!refused, true, "the active world has no Delete button at all");
  // Greyed out, but NOT `disabled`: a disabled button takes no hover and no focus, so
  // the explanation would be unreachable by mouse and keyboard alike.
  eq(refused.disabled, false, "the refused Delete is hard-disabled");
  eq(refused.getAttribute("aria-disabled"), "true", "the refused Delete is not marked disabled");

  const bubble = p.bubbleOf(refused);
  eq(bubble.hidden, true, "the explanation is showing before anyone asked for it");
  eq(refused.getAttribute("aria-describedby"), bubble.id, "the button does not name its own explanation");
  eq(bubble.textContent.indexOf("Load a different world first") !== -1, true,
     "the explanation does not say what to do: " + bubble.textContent);

  p.hover(refused, "mouseover");
  eq(bubble.hidden, false, "hovering did not show the explanation");
  p.hover(refused, "mouseout");
  eq(bubble.hidden, true, "the explanation stayed up after the mouse left");

  // The keyboard gets there too.
  refused.focus();
  eq(bubble.hidden, false, "focusing did not show the explanation");
  refused.blur();
  eq(bubble.hidden, true, "the explanation stayed up after focus left");
});

check("a_refused_delete_does_nothing_when_pressed", async () => {
  // The trap here: jsdom's `dialog.open` is always false and its stub `confirm()`
  // returns undefined, so asserting either would pass whether or not the guard exists.
  // Confirm is therefore stubbed to say YES -- if the click ever reaches askDelete,
  // the delete goes through and this fails.
  const p = makePage();
  p.status("stopped");
  await p.settle();
  p.win.confirm = () => true;

  const refused = p.doc.querySelector('button[data-delete-world="Dedicated"]');
  const before = p.fetches.length;
  refused.click();
  await p.settle();

  eq(p.fetches.slice(before).filter((u) => u.indexOf("/api/worlds/delete") === 0), [],
     "pressing a refused Delete deleted the active world");
});

check("the_new_world_field_explains_itself_on_focus", async () => {
  const p = makePage();
  p.status("stopped");
  await p.settle();
  const field = p.doc.getElementById("world-new-name");
  const bubble = p.doc.getElementById("world-new-hint");

  eq(bubble.hidden, true, "the hint is showing before the field was touched");
  eq(field.getAttribute("aria-describedby"), bubble.id, "the field does not name its hint");
  field.focus();
  eq(bubble.hidden, false, "focusing the field did not show the hint");
  eq(bubble.textContent.indexOf("random seed") !== -1, true,
     "the hint does not explain what Create does: " + bubble.textContent);
  field.blur();
  eq(bubble.hidden, true, "the hint stayed up after the field lost focus");
});

check("a_deletable_world_is_never_deleted_straight_off_the_click", async () => {
  // jsdom implements no <dialog>, so this drives the fallback -- which is also the
  // path any browser without <dialog> takes. Either way the rule is the same: a click
  // on Delete asks, and a refusal deletes nothing.
  const p = makePage();
  p.status("stopped");
  await p.settle();
  eq(typeof p.doc.getElementById("delete-dialog").showModal, "undefined",
     "precondition: this environment has no <dialog>, so the fallback is under test");

  const asked = [];
  p.win.confirm = (text) => { asked.push(text); return false; };
  let before = p.fetches.length;
  p.doc.querySelector('button[data-delete-world="Oldsave"]').click();
  eq(asked.length, 1, "Delete did not ask anything");
  eq(asked[0].indexOf("Oldsave") !== -1, true, "the question does not name the world: " + asked[0]);
  eq(p.fetches.slice(before), [], "saying no still deleted the world");

  // ...and saying yes goes through, exactly once.
  p.win.confirm = () => true;
  before = p.fetches.length;
  p.doc.querySelector('button[data-delete-world="Oldsave"]').click();
  await p.settle();
  const posted = p.fetches.slice(before).filter((u) => u.indexOf("/api/worlds/delete") === 0);
  eq(posted.length, 1, "confirming did not send exactly one delete");
});

check("the_confirmation_is_wired_for_browsers_that_have_it", async () => {
  // What jsdom cannot run is still worth pinning: the dialog, its buttons, and the
  // fact that Cancel is the one the keyboard lands on. Driven for real in a browser.
  const p = makePage();
  const dialog = p.doc.getElementById("delete-dialog");
  eq(!!dialog, true, "there is no confirmation dialog in the page");
  eq(dialog.hasAttribute("open"), false, "the dialog ships open");
  const cancel = p.doc.getElementById("btn-delete-cancel");
  const confirm = p.doc.getElementById("btn-delete-confirm");
  eq(!!cancel && !!confirm, true, "the dialog is missing a button");
  eq(cancel.hasAttribute("autofocus"), true, "Cancel is not what focus lands on");
  eq(confirm.hasAttribute("autofocus"), false, "the destructive button takes focus");
  eq(confirm.className.indexOf("danger") !== -1, true, "the destructive button is not marked as one");
});

check("opening_straight_onto_the_mods_tab_does_not_kill_the_dashboard", async () => {
  // The Mods tab can be the tab the page OPENS on, which is earlier than the first
  // /api/worlds answer -- so the world list it wants is still null at that moment.
  // Throwing there happens at module scope and takes the rest of the IIFE with it:
  // no socket, no console, no buttons. Landing on the tab is the whole test.
  const p = makePage({ stored: "mods" });
  await p.settle();

  eq(p.selected(), ["mods"], "the stored tab was not restored");
  eq(p.shown(), ["panel-mods"], "visible panels");
  // The proof that init survived: everything after initTabs still ran.
  eq(p.sockets.length, 1, "the socket never opened -- init threw");
  p.status("stopped");
  eq(p.doc.getElementById("btn-start").disabled, false, "the controls never came alive");
  p.tab("console").click();
  eq(p.shown(), ["panel-console"], "the tabs stopped working");
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
  for (const name of ["console", "settings", "worlds", "mods", "players"]) {
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

// ------------------------------------------------------- the backup schedule
//
// Three ways to say how often, two of which share a pair of radios and one of which
// is a time on the clock. Which control is showing what is entirely runtime, and the
// mode is the only thing that says which radio is right -- the interval alone cannot,
// because a daily schedule still carries one.

const SCHEDULE = (over = {}) => ({
  enabled: true, mode: "interval", interval_hours: 24, daily_time: "03:00",
  keep_per_world: 7, last_run_at: null, last_error: "", ...over,
});

// The backups card is fed by /api/backups, not by the status socket, so a schedule
// reaches the panel through a refresh rather than through a push.
const withSchedule = (schedule) => {
  const p = makePage();
  p.win.fetch = (path) => {
    p.fetches.push(String(path));
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ schedule, backups: [], error: "", loaded_world: "Dedicated" }),
    });
  };
  p.doc.getElementById("btn-backups-refresh").click();
  return p;
};
const settled = () => new Promise((resolve) => setTimeout(resolve, 0));

check("a_daily_schedule_selects_the_time_radio_and_shows_the_time", async () => {
  const p = withSchedule(SCHEDULE({ mode: "daily", daily_time: "13:30" }));
  await settled();
  eq(p.doc.getElementById("backup-every-at").checked, true, "the 'every day at' radio");
  eq(p.doc.getElementById("backup-daily-time").value, "13:30", "the time shown");
  eq(p.doc.getElementById("backup-every-day").checked, false, "the 24h radio");
  eq(p.doc.getElementById("backup-every-custom").checked, false, "the custom radio");
});

check("an_interval_schedule_leaves_the_time_radio_alone", async () => {
  // The time is still carried while an interval is in force: switching to "every day
  // at" and back has to find each setting where it was left.
  const p = withSchedule(SCHEDULE({ interval_hours: 6, daily_time: "13:30" }));
  await settled();
  eq(p.doc.getElementById("backup-every-at").checked, false, "the 'every day at' radio");
  eq(p.doc.getElementById("backup-every-custom").checked, true, "the custom radio");
  eq(p.doc.getElementById("backup-daily-time").value, "13:30", "the unused time");
});

check("the_default_schedule_still_selects_the_first_radio", async () => {
  const p = withSchedule(SCHEDULE({ interval_hours: 24 }));
  await settled();
  eq(p.doc.getElementById("backup-every-day").checked, true, "the 24h radio");
  eq(p.doc.getElementById("backup-every-at").checked, false, "the 'every day at' radio");
});

check("touching_the_time_field_picks_that_mode", async () => {
  // Setting the time is how most people will choose the mode, so it must not need
  // the radio to be found first -- the same courtesy the hours field gets.
  const p = makePage();
  p.doc.getElementById("backup-daily-time").dispatchEvent(new p.win.Event("focus"));
  eq(p.doc.getElementById("backup-every-at").checked, true, "the radio after focus");
});

check("turning_backups_off_disables_the_time_field", async () => {
  const p = makePage();
  const box = p.doc.getElementById("backup-enabled");
  box.checked = false;
  box.dispatchEvent(new p.win.Event("change"));
  eq(p.doc.getElementById("backup-daily-time").disabled, true, "the time field");
  eq(p.doc.getElementById("backup-every-at").disabled, true, "the time radio");
});

check("saving_posts_both_the_mode_and_the_time", async () => {
  // A form's submit handler is bound to the form, and the button has to be inside it.
  // This is the shape of the bug that left `Add mod` inert while 506 tests passed.
  const p = makePage();
  const sent = [];
  p.win.fetch = (path, init) => {
    sent.push({ path: String(path), body: JSON.parse((init && init.body) || "{}") });
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
  };
  p.doc.getElementById("backup-daily-time").value = "01:30";
  p.doc.getElementById("backup-every-at").checked = true;
  p.doc.getElementById("backup-schedule-form")
    .dispatchEvent(new p.win.Event("submit", { bubbles: true, cancelable: true }));
  const post = sent.find((s) => s.path.indexOf("/api/backups/schedule") >= 0);
  if (!post) { throw new Error("nothing was posted to the schedule endpoint"); }
  eq(post.body.mode, "daily", "posted mode");
  eq(post.body.daily_time, "01:30", "posted time");
  eq(post.body.interval_hours, 24, "the interval carried alongside it");
});

// -------------------------------------------- the game server's own backups
//
// The image zips the whole worlds folder, so its archives are named by time and can
// hold several worlds. The row names what is inside; Delete asks before it removes
// anything, for these and for the manager's own archives alike.

const GAME_BACKUP = (over = {}) => ({
  name: "worlds-20260916-1041.zip", world: "", kind: "game", size_bytes: 5000000,
  size: "4.8 MB", taken_at: 1789550460, restorable: true, deletable: true,
  contains: ["kaki", "Kakui"], ...over,
});

const withBackups = (backups) => {
  const p = makePage();
  p.backupPosts = [];
  p.win.fetch = (path, init) => {
    const url = String(path);
    p.fetches.push(url);
    if (init && init.method === "POST") {
      let body = null;
      try { body = JSON.parse(init.body); } catch (err) { body = null; }
      p.backupPosts.push({ path: url, body });
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        schedule: SCHEDULE(), backups, error: "", loaded_world: "Dedicated",
      }),
    });
  };
  p.doc.getElementById("btn-backups-refresh").click();
  return p;
};

check("a_game_backup_row_names_the_worlds_it_holds", async () => {
  const p = withBackups([
    GAME_BACKUP(),
    GAME_BACKUP({ name: "worlds-20260914-1216.zip", contains: [] }),
    GAME_BACKUP({ name: "SCHEDULED-Kakui-20260925-012200.zip", world: "Kakui",
                  kind: "scheduled", contains: [] }),
  ]);
  await settled();
  const cells = [...p.doc.querySelectorAll("#backups-body .backup-world")]
    .map((cell) => cell.textContent);
  eq(cells, ["kaki, Kakui", "—", "Kakui"], "the world column");
});

check("a_game_backup_offers_delete", async () => {
  const p = withBackups([GAME_BACKUP()]);
  await settled();
  const remove = p.doc.querySelector(
    'button[data-backup-delete="worlds-20260916-1041.zip"]');
  eq(!!remove, true, "a game-server backup has no Delete button");
});

check("a_backup_is_never_deleted_straight_off_the_click", async () => {
  // jsdom has no <dialog>, so this drives the fallback, which any browser without
  // <dialog> takes too. The rule is the same either way: Delete asks first.
  const p = withBackups([GAME_BACKUP()]);
  await settled();
  eq(typeof p.doc.getElementById("backup-delete-dialog").showModal, "undefined",
     "precondition: this environment has no <dialog>, so the fallback is under test");

  const asked = [];
  p.win.confirm = (text) => { asked.push(text); return false; };
  p.doc.querySelector('button[data-backup-delete="worlds-20260916-1041.zip"]').click();
  await settled();
  eq(asked.length, 1, "Delete did not ask anything");
  eq(asked[0].indexOf("worlds-20260916-1041.zip") !== -1, true,
     "the question does not name the archive: " + asked[0]);
  eq(asked[0].indexOf("Kakui") !== -1, true,
     "the question does not say which worlds go with it: " + asked[0]);
  eq(p.backupPosts, [], "saying no still deleted the backup");

  p.win.confirm = () => true;
  p.doc.querySelector('button[data-backup-delete="worlds-20260916-1041.zip"]').click();
  await settled();
  eq(p.backupPosts, [{ path: "/api/backups/delete",
                       body: { name: "worlds-20260916-1041.zip" } }],
     "confirming did not send exactly one delete for that archive");
});

check("the_backup_delete_dialog_is_wired_for_browsers_that_have_it", async () => {
  const p = makePage();
  const dialog = p.doc.getElementById("backup-delete-dialog");
  eq(!!dialog, true, "there is no backup delete dialog in the page");
  eq(dialog.hasAttribute("open"), false, "the dialog ships open");
  const cancel = p.doc.getElementById("btn-backup-delete-cancel");
  const confirm = p.doc.getElementById("btn-backup-delete-confirm");
  eq(!!cancel && !!confirm, true, "the dialog is missing a button");
  eq(cancel.hasAttribute("autofocus"), true, "Cancel is not what focus lands on");
  eq(confirm.hasAttribute("autofocus"), false, "the destructive button takes focus");
  eq(confirm.className.indexOf("danger") !== -1, true,
     "the destructive button is not marked as one");
});

// ------------------------------------------------------------ the players tab
//
// The roster is fed by /api/players, read once when the page loads, so every case
// here lets that first read land before looking.

const playersPage = async (payload, opts = {}) => {
  const p = makePage(opts);
  if (payload) { p.setPlayers(payload); }
  // The page read the roster on load; with a custom payload, read it again.
  if (payload) { p.doc.getElementById("btn-players-refresh").click(); }
  await p.settle();
  await p.settle();
  return p;
};
const rows = (p) => [...p.doc.querySelectorAll("#players-body tr")];
const box = (p, row, kind) => rows(p)[row].querySelector(`input[data-player-list="${kind}"]`);
const tick = (p, node, on) => {
  node.checked = on;
  node.dispatchEvent(new p.win.Event("change", { bubbles: true }));
};

check("the_players_tab_shows_its_panel", async () => {
  const p = await playersPage(null);
  p.tab("players").click();
  eq(p.selected(), ["players"], "selected tab");
  eq(p.shown(), ["panel-players"], "visible panels");
  eq(rows(p).length, 4, "roster rows drawn from /api/players");
  eq(rows(p)[0].children[0].textContent.indexOf("Ragnar") !== -1, true, "the name is shown");
});

check("a_roster_time_reads_on_the_server_clock_whatever_the_browser_zone", async () => {
  // The instant: 2023-11-14 22:14:00 UTC. The server is at +05:30, so its wall clock
  // reads Wed 15 Nov 03:44. The page is then run under four browser zones, none of
  // them +05:30 -- a renderer reading local time would answer differently in each.
  const EPOCH = 1700000040;
  const OFFSET = 330;
  const WANT = "Wed 15 Nov 03:44";
  const ZONES = ["UTC", "Pacific/Chatham", "America/St_Johns", "America/Los_Angeles"];
  const before = process.env.TZ;
  const localHours = [];
  try {
    for (const zone of ZONES) {
      process.env.TZ = zone;
      const p = makePage();
      // Proof the zone actually changed inside the page, or this case proves nothing.
      localHours.push(new p.win.Date(EPOCH * 1000).getHours());
      p.push({
        type: "status",
        status: { phase: "ready", message: "ready", container_state: "running",
                  container_exists: true, started_at: null },
        server_clock: { epoch: EPOCH, offset_minutes: OFFSET, zone: "Test/Zone" },
        settings: SETTINGS_ROWS, settings_error: null, modifiers: {},
      });
      p.setPlayers(PLAYERS());
      p.doc.getElementById("btn-players-refresh").click();
      await p.settle();
      await p.settle();

      const cells = rows(p)[0].children;
      eq(cells[2].textContent, WANT, `last seen under browser zone ${zone}`);
      eq(rows(p)[2].children[2].textContent, "never", `a never-seen player under ${zone}`);
      // ...and it is the wall time the header clock shows for the same instant.
      const time = p.doc.getElementById("clock-time").textContent;
      const date = p.doc.getElementById("clock-date").textContent;
      eq(WANT, date.split(" ").slice(0, 3).join(" ") + " " + time.slice(0, 5),
         `the roster and the header clock disagree under ${zone}`);
    }
  } finally {
    if (before === undefined) { delete process.env.TZ; } else { process.env.TZ = before; }
  }
  eq(new Set(localHours).size > 1, true,
     "precondition: the browser zone never changed, so the zone-independence is untested");
});

check("the_permitted_editor_keeps_parked_players_as_disabled_lines", async () => {
  // While the list is off every entry is parked, and `ids` is empty. A box seeded from
  // `ids` alone would be empty, and saving it would erase everyone on the list.
  const p = await playersPage(null);
  const permitted = p.doc.getElementById("raw-permitted-text");
  eq(permitted.value,
     `// disabled-by-manager ${RAGNAR}\n// disabled-by-manager V_2222\n`,
     "the permitted editor");
  eq(permitted.value.indexOf("List permitted") === -1, true, "the game's header note is shown");
  eq(p.doc.getElementById("raw-admin-text").value, `${RAGNAR}\n`, "the admin editor");

  // Saving it untouched writes back exactly what the file holds.
  p.doc.querySelector('[data-raw-save="permitted"]').click();
  await p.settle();
  const saved = p.posts.filter((x) => x.path === "/api/players/raw");
  eq(saved.length, 1, "Save did not post the permitted list");
  eq(saved[0].body, { kind: "permitted", text: permitted.value }, "what Save posted");
});

check("nothing_on_the_page_offers_a_kick", async () => {
  // Vanilla Valheim has no channel for the manager to send one. Nothing may offer one
  // -- not a button, not a disabled placeholder, not an attribute. The word may appear
  // only in the one note that says it is NOT available here, and why.
  const p = await playersPage(PLAYERS());
  p.tab("players").click();
  const note = p.doc.getElementById("online-limits");
  eq(!!note, true, "there is no note explaining what cannot be done from here");
  const outside = p.doc.documentElement.cloneNode(true);
  const copy = outside.querySelector("#online-limits");
  if (copy) { copy.remove(); }
  eq(/kick/i.test(outside.textContent), false, "text outside the limits note mentions a kick");
  for (const node of p.doc.querySelectorAll("button, input, a, select, option, label")) {
    eq(/kick/i.test(node.textContent || ""), false,
       `a <${node.tagName.toLowerCase()}> offers a kick`);
  }
  for (const node of p.doc.querySelectorAll("*")) {
    for (const attr of node.attributes) {
      if (/kick/i.test(attr.value)) {
        throw new Error(`<${node.tagName.toLowerCase()} ${attr.name}="${attr.value}"> mentions a kick`);
      }
    }
  }
});

// ---------------------------------------------------------------- online now

const ONLINE = (over = {}) => PLAYERS({
  players: [
    { id: RAGNAR, platform_id: "76561198012345678", file_id: RAGNAR, name: "Ragnar",
      first_seen: 1699990000, last_seen: 1700000040, last_world: "Dedicated", seen: true,
      is_admin: false, is_banned: false, is_permitted: false,
      online: true, online_since: 1700000000 },
    { id: "V_1111", platform_id: null, file_id: "V_1111", name: null,
      first_seen: null, last_seen: null, last_world: null, seen: false,
      is_admin: false, is_banned: true, is_permitted: false,
      online: false, online_since: null },
  ],
  ...over,
});
const onlineRows = (p) => [...p.doc.querySelectorAll("#online-body tr")];

check("online_players_are_listed_with_what_can_be_done", async () => {
  const p = await playersPage(ONLINE());
  p.tab("players").click();
  const listed = onlineRows(p);
  eq(listed.length, 1, "the online list");
  eq(listed[0].textContent.indexOf("Ragnar") !== -1, true, "the online row's name");
  eq(p.doc.getElementById("online-count").textContent, "1", "the count");
  eq(p.doc.getElementById("online-empty").hidden, true, "the empty note is showing");
  eq(!!listed[0].querySelector("button[data-online-ban]"), true, "no Ban button");
  eq(!!listed[0].querySelector("button[data-online-admin]"), true, "no Make admin button");
});

check("nobody_online_says_so", async () => {
  const p = await playersPage(PLAYERS());
  p.tab("players").click();
  eq(onlineRows(p).length, 0, "rows in the online list");
  eq(p.doc.getElementById("online-empty").hidden, false, "the empty note");
  eq(p.doc.getElementById("online-count").textContent, "0", "the count");
});

check("banning_an_online_player_asks_first", async () => {
  const p = await playersPage(ONLINE());
  p.tab("players").click();
  eq(typeof p.doc.getElementById("ban-dialog").showModal, "undefined",
     "precondition: this environment has no <dialog>, so the fallback is under test");
  const asked = [];
  p.win.confirm = (text) => { asked.push(text); return false; };
  const before = p.posts.length;
  onlineRows(p)[0].querySelector("button[data-online-ban]").click();
  await p.settle();
  eq(asked.length, 1, "Ban did not ask anything");
  eq(asked[0].indexOf("Ragnar") !== -1, true, "the question does not name the player: " + asked[0]);
  eq(p.posts.slice(before), [], "saying no still banned them");

  p.win.confirm = () => true;
  onlineRows(p)[0].querySelector("button[data-online-ban]").click();
  await p.settle();
  eq(p.posts.slice(before),
     [{ path: "/api/players/list", body: { kind: "banned", file_id: RAGNAR, member: true } }],
     "confirming did not send exactly one ban");
});

check("making_an_online_player_admin_posts_the_list_change", async () => {
  const p = await playersPage(ONLINE());
  p.tab("players").click();
  const before = p.posts.length;
  onlineRows(p)[0].querySelector("button[data-online-admin]").click();
  await p.settle();
  eq(p.posts.slice(before),
     [{ path: "/api/players/list", body: { kind: "admin", file_id: RAGNAR, member: true } }],
     "Make admin did not send exactly one change");
});

check("the_ban_dialog_is_wired_for_browsers_that_have_it", async () => {
  const p = makePage();
  const dialog = p.doc.getElementById("ban-dialog");
  eq(!!dialog, true, "there is no ban dialog in the page");
  eq(dialog.hasAttribute("open"), false, "the dialog ships open");
  const cancel = p.doc.getElementById("btn-ban-cancel");
  const confirm = p.doc.getElementById("btn-ban-confirm");
  eq(!!cancel && !!confirm, true, "the dialog is missing a button");
  eq(cancel.hasAttribute("autofocus"), true, "Cancel is not what focus lands on");
  eq(confirm.className.indexOf("danger") !== -1, true, "Ban is not marked as destructive");
});

check("a_row_without_a_usable_id_cannot_be_ticked_and_says_why", async () => {
  const p = await playersPage(null);
  const row = rows(p)[1];
  const boxes = [...row.querySelectorAll("input[data-player-list]")];
  eq(boxes.length, 3, "the row has its three boxes");
  eq(boxes.map((b) => b.disabled), [true, true, true], "the boxes are live");
  const note = p.doc.getElementById(boxes[0].getAttribute("aria-describedby") || "-");
  eq(!!note && row.contains(note), true, "the explanation is not on the row");
  eq(note.textContent.indexOf("F2") !== -1, true, "the explanation does not say where the ID is: " + note.textContent);

  // Forced anyway: nothing goes out, and the box goes back.
  tick(p, boxes[0], true);
  await p.settle();
  eq(p.posts, [], "a row with no ID sent something");
  eq(box(p, 1, "admin").checked, false, "the box stayed ticked");
});

check("a_refused_tick_is_put_back_from_a_fresh_read", async () => {
  const p = await playersPage(null);
  const refusal = "That ID contains a newline. Add it by hand from the in-game F2 panel.";
  // The file changed elsewhere since the page last read it: the fresh read is the only
  // place the right answer can come from.
  const fresh = PLAYERS();
  fresh.players[0] = { ...fresh.players[0], is_admin: false };
  p.answerPlayers((url, init) => {
    if (url === "/api/players/list") { return { status: 400, payload: { error: refusal } }; }
    if (!init || !init.method) { return { status: 200, payload: fresh }; }
    return null;
  });
  const before = p.fetches.length;
  tick(p, box(p, 0, "banned"), true);
  await p.settle();
  await p.settle();

  eq(p.doc.getElementById("players-error").hidden, false, "the refusal is not shown");
  eq(p.doc.getElementById("players-error").textContent, refusal, "the refusal was reworded");
  eq(box(p, 0, "banned").checked, false, "the refused tick is still showing");
  eq(p.fetches.slice(before), ["/api/players/list", "/api/players"], "requests after the refusal");
  eq(box(p, 0, "admin").checked, false, "the table was not repainted from the fresh read");
});

check("turning_the_permitted_list_on_asks_first", async () => {
  // jsdom has no <dialog>, so this drives the fallback -- the path any browser without
  // one takes. The rule is the same either way: ask, and a no sends nothing.
  const p = await playersPage(null);
  eq(typeof p.doc.getElementById("whitelist-dialog").showModal, "undefined",
     "precondition: this environment has no <dialog>, so the fallback is under test");
  const toggle = p.doc.getElementById("whitelist-toggle");
  const asked = [];
  p.win.confirm = (text) => { asked.push(text); return false; };
  tick(p, toggle, true);
  await p.settle();
  eq(asked.length, 1, "switching it on did not ask");
  eq(asked[0].indexOf("Ragnar") !== -1, true, "the question does not say who is on the list: " + asked[0]);
  eq(asked[0].indexOf("Everyone else is turned away") !== -1, true, "the question does not say what happens");
  eq(p.posts, [], "saying no still switched it on");
  eq(toggle.checked, false, "the switch shows on after a no");

  p.win.confirm = () => true;
  tick(p, toggle, true);
  await p.settle();
  eq(p.posts.map((x) => [x.path, x.body]), [["/api/players/whitelist", { enabled: true }]],
     "saying yes did not send exactly one switch-on");

  // Off is the safe direction: no question.
  const onPage = await playersPage(PLAYERS({ whitelist_enabled: true }));
  const off = [];
  onPage.win.confirm = (text) => { off.push(text); return false; };
  tick(onPage, onPage.doc.getElementById("whitelist-toggle"), false);
  await onPage.settle();
  eq(off, [], "switching it off asked");
  eq(onPage.posts.map((x) => x.body), [{ enabled: false }], "switching it off was not sent");

  // The dialog itself, for browsers that have one: Cancel is where focus lands.
  eq(p.doc.getElementById("btn-whitelist-cancel").hasAttribute("autofocus"), true,
     "Cancel is not what focus lands on");
  eq(p.doc.getElementById("btn-whitelist-confirm").hasAttribute("autofocus"), false,
     "the switch-on button takes focus");
});

check("ticking_permitted_while_the_list_is_off_goes_to_the_manager_like_the_others", async () => {
  // Keeping the whitelist off is the MANAGER's rule now (a permitted add while the
  // list is off is parked, never written active), so the column posts exactly like
  // Admin and Banned -- and says, beside itself, that a tick locks nobody out.
  const p = await playersPage(null);
  const note = p.doc.getElementById("permitted-note");
  eq(note.hidden, false, "the note beside Permitted is hidden while the list is off");
  eq(/nobody is locked out/i.test(note.textContent), true, "the note does not say nobody is locked out: " + note.textContent);
  eq(box(p, 0, "permitted").checked, true, "a parked player is not shown as on the list");

  tick(p, box(p, 2, "permitted"), true);
  await p.settle();
  eq(p.posts.map((x) => [x.path, x.body]),
     [["/api/players/list", { kind: "permitted", file_id: "V_1111", member: true }]],
     "what the Permitted tick sent");

  // Adding by ID goes straight to the manager too.
  const before = p.posts.length;
  p.doc.getElementById("player-add-kind").value = "permitted";
  p.doc.getElementById("player-add-id").value = "V_3333";
  p.doc.getElementById("player-add-form")
    .dispatchEvent(new p.win.Event("submit", { bubbles: true, cancelable: true }));
  await p.settle();
  eq(p.posts.slice(before).map((x) => [x.path, x.body]),
     [["/api/players/add", { kind: "permitted", id: "V_3333" }]], "what Add sent");

  // With the list on, the note goes away: a tick there IS in force.
  const on = await playersPage(PLAYERS({ whitelist_enabled: true }));
  eq(on.doc.getElementById("permitted-note").hidden, true, "the 'nobody is locked out' note shows while the list is on");
});

check("saving_active_permitted_lines_while_the_list_is_off_asks_first", async () => {
  // The raw editor writes exactly what is typed, so it is the one other way to switch
  // the whitelist on -- and gets the same question the switch does.
  const p = await playersPage(null);
  const editor = p.doc.getElementById("raw-permitted-text");
  editor.value = editor.value + "V_3333\n";
  editor.dispatchEvent(new p.win.Event("input", { bubbles: true }));
  const asked = [];
  p.win.confirm = (text) => { asked.push(text); return false; };
  p.doc.querySelector('[data-raw-save="permitted"]').click();
  await p.settle();
  eq(asked.length, 1, "saving an active line did not ask");
  eq(asked[0].indexOf("V_3333") !== -1, true, "the question does not name who would be let in: " + asked[0]);
  eq(p.posts, [], "saying no still saved it");
  eq(editor.value.indexOf("V_3333") !== -1, true, "saying no threw away what was typed");

  p.win.confirm = () => true;
  p.doc.querySelector('[data-raw-save="permitted"]').click();
  await p.settle();
  eq(p.posts.map((x) => x.path), ["/api/players/raw"], "saying yes did not save exactly once");

  // Parked-only text is not switching anything on: no question.
  const quiet = await playersPage(null);
  const q = [];
  quiet.win.confirm = (text) => { q.push(text); return false; };
  quiet.doc.querySelector('[data-raw-save="permitted"]').click();
  await quiet.settle();
  eq(q, [], "saving parked lines only asked");
  eq(quiet.posts.length, 1, "saving parked lines only was not sent");
});

check("a_list_variable_in_valheim_env_is_named_in_a_warning", async () => {
  const quiet = await playersPage(null);
  eq(quiet.doc.getElementById("players-env-conflict").hidden, true, "a warning with nothing set");

  const p = await playersPage(PLAYERS({ list_env_conflicts: ["ADMINLIST_IDS"] }));
  const banner = p.doc.getElementById("players-env-conflict");
  eq(banner.hidden, false, "the warning is not shown");
  eq(banner.textContent.indexOf("ADMINLIST_IDS") !== -1, true, "the warning does not name the variable");
  eq(banner.textContent.indexOf("valheim.env") !== -1, true, "the warning does not say where it is set");
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

// Close every window so their timers stop and node can exit on its own. Deliberately
// not `process.exit()`: stdout is a pipe when pytest captures it, and exiting can
// truncate a write that has not flushed -- which would turn this into a harness that
// reports nothing instead of one that reports everything and hangs.
for (const dom of PAGES) { dom.window.close(); }
