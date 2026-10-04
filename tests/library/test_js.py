"""The two browser helpers, run under Node with a minimal DOM stub (skipped without Node)."""
from __future__ import annotations

import json
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def asset(name: str) -> str:
    return resources.files("appext").joinpath("js", name).read_text(encoding="utf-8")


def run_node(tmp_path: Path, script: str) -> dict:
    (tmp_path / "client.mjs").write_text(asset("client.js"))
    (tmp_path / "bridge.js").write_text(asset("bridge.js"))
    (tmp_path / "run.mjs").write_text(script)
    done = subprocess.run([NODE, str(tmp_path / "run.mjs")], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


PRELUDE = """
import { readFileSync } from "node:fs";
const out = {};
const storage = new Map();
globalThis.sessionStorage = { getItem: (k) => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, String(v)) };
globalThis.navigations = [];
globalThis.window = { location: { origin: "https://demo.apps.test", pathname: "/reports", search: "?tab=2", assign: (u) => navigations.push(u) } };
globalThis.requests = [];
globalThis.respond = null;
globalThis.fetch = async (input, init) => { requests.push({ input: String(input), headers: Object.fromEntries(new Headers(init.headers)), credentials: init.credentials, method: init.method || "GET" }); return respond(); };
"""


def test_extfetch_adds_the_csrf_header_and_same_origin_credentials(tmp_path):
    result = run_node(
        tmp_path,
        PRELUDE
        + """
const { extFetch } = await import("./client.mjs");
respond = () => new Response(JSON.stringify({ ok: 1 }), { status: 200 });
const r = await extFetch("/api/x", { method: "POST", headers: { "X-Other": "1" } });
out.status = r.status; out.request = requests[0]; out.navigations = navigations;
console.log(JSON.stringify(out));
""",
    )
    assert result["status"] == 200 and result["navigations"] == []
    assert result["request"]["headers"]["x-appext-csrf"] == "1"
    assert result["request"]["headers"]["x-other"] == "1" and result["request"]["headers"]["accept"] == "application/json"
    assert result["request"]["credentials"] == "same-origin" and result["request"]["method"] == "POST"


def test_extfetch_navigates_top_level_on_401_with_return_to_the_current_page(tmp_path):
    result = run_node(
        tmp_path,
        PRELUDE
        + """
const { extFetch } = await import("./client.mjs");
respond = () => new Response(JSON.stringify({ error: "unauthenticated", login_url: "/auth/login?return_to=%2F" }), { status: 401 });
let settled = false;
extFetch("/api/x").then(() => { settled = true; });
await new Promise((r) => setTimeout(r, 50));
out.navigations = navigations; out.settled = settled;
console.log(JSON.stringify(out));
""",
    )
    assert result["navigations"] == ["/auth/login?return_to=%2Freports%3Ftab%3D2"]
    assert result["settled"] is False  # the page is going away: no caller renders an error for it


def test_extfetch_carries_the_consent_scopes_through(tmp_path):
    result = run_node(
        tmp_path,
        PRELUDE
        + """
const { extFetch } = await import("./client.mjs");
respond = () => new Response(JSON.stringify({ error: "consent_required", login_url: "/auth/login?return_to=%2F&scope=svc-a+svc-b" }), { status: 401 });
extFetch("/api/x");
await new Promise((r) => setTimeout(r, 50));
out.navigations = navigations;
console.log(JSON.stringify(out));
""",
    )
    (target,) = result["navigations"]
    assert "scope=svc-a+svc-b" in target and "return_to=%2Freports%3Ftab%3D2" in target


@pytest.mark.parametrize(
    "body",
    ['{"login_url": "https://evil.test/phish"}', '{"login_url": "//evil.test"}', '{"login_url": "/somewhere/else"}', "not json", "{}"],
)
def test_extfetch_only_follows_a_login_url_on_its_own_origin(tmp_path, body):
    result = run_node(
        tmp_path,
        PRELUDE
        + f"""
const {{ extFetch }} = await import("./client.mjs");
respond = () => new Response({json.dumps(body)}, {{ status: 401 }});
const r = await extFetch("/api/x");
out.status = r.status; out.navigations = navigations;
console.log(JSON.stringify(out));
""",
    )
    assert result["navigations"] == [] and result["status"] == 401


def test_extfetch_gives_up_instead_of_looping_the_login(tmp_path):
    result = run_node(
        tmp_path,
        PRELUDE
        + """
const { extFetch } = await import("./client.mjs");
respond = () => new Response(JSON.stringify({ login_url: "/auth/login?return_to=%2F" }), { status: 401 });
for (let i = 0; i < 3; i++) { extFetch("/api/x"); await new Promise((r) => setTimeout(r, 20)); }
const fourth = await extFetch("/api/x");   // the guard allows three navigations in 30 s
out.navigations = navigations.length; out.status = fourth.status;
console.log(JSON.stringify(out));
""",
    )
    assert result["navigations"] == 3 and result["status"] == 401


def test_extfetch_works_without_session_storage(tmp_path):
    result = run_node(
        tmp_path,
        PRELUDE
        + """
globalThis.sessionStorage = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
const { extFetch } = await import("./client.mjs");
respond = () => new Response(JSON.stringify({ login_url: "/auth/login?return_to=%2F" }), { status: 401 });
extFetch("/api/x");
await new Promise((r) => setTimeout(r, 30));
out.navigations = navigations.length;
console.log(JSON.stringify(out));
""",
    )
    assert result["navigations"] == 1


BRIDGE_PRELUDE = """
import { readFileSync } from "node:fs";
const out = {};
globalThis.window = globalThis;
const code = readFileSync(new URL("./bridge.js", import.meta.url), "utf8");
const load = () => (0, eval)(code);
"""


def test_bridge_sends_the_three_commands_as_json(tmp_path):
    result = run_node(
        tmp_path,
        BRIDGE_PRELUDE
        + """
const sent = [];
globalThis.AppExtBridge = { postMessage: (m) => sent.push(m) };
load();
out.inApp = AppExt.inApp;
out.results = [AppExt.close(), AppExt.setTitle("Reports"), AppExt.openExternal("https://example.com/x")];
out.sent = sent.map((m) => JSON.parse(m));
console.log(JSON.stringify(out));
""",
    )
    assert result["inApp"] is True and result["results"] == [True, True, True]
    assert result["sent"] == [
        {"command": "close"},
        {"command": "setTitle", "title": "Reports"},
        {"command": "openExternal", "url": "https://example.com/x"},
    ]


def test_bridge_is_a_quiet_no_op_outside_the_app(tmp_path):
    result = run_node(
        tmp_path,
        BRIDGE_PRELUDE
        + """
load();
out.inApp = AppExt.inApp;
out.results = [AppExt.close(), AppExt.setTitle("x"), AppExt.openExternal("https://example.com")];
const unsubscribe = AppExt.onTheme(() => {});
out.unsubscribe = typeof unsubscribe;
console.log(JSON.stringify(out));
""",
    )
    assert result["inApp"] is False and result["results"] == [False, False, False] and result["unsubscribe"] == "function"


def test_bridge_has_no_data_commands(tmp_path):
    result = run_node(
        tmp_path,
        BRIDGE_PRELUDE
        + """
load();
out.api = Object.keys(AppExt).sort();
console.log(JSON.stringify(out));
""",
    )
    # Nothing here can return a token or a user: only commands that go out, and subscriptions for what the app pushes.
    assert result["api"] == ["close", "emit", "inApp", "onLanguage", "onTheme", "openExternal", "setTitle"]


def test_bridge_events_reach_subscribers_and_late_subscribers(tmp_path):
    result = run_node(
        tmp_path,
        BRIDGE_PRELUDE
        + """
load();
const seen = [];
const off = AppExt.onTheme((t) => seen.push("early:" + t));
AppExt.emit("theme", "dark");
AppExt.onTheme((t) => seen.push("late:" + t));          // late subscribers get the stored value at once
AppExt.emit("language", "de");
AppExt.onLanguage((l) => seen.push("lang:" + l));
off();
AppExt.emit("theme", "light");
AppExt.emit("unknown", "x");                              // ignored
AppExt.emit("theme", 5);                                  // ignored: not a string
AppExt.onTheme(() => { throw new Error("a broken subscriber"); });  // must not break the others
AppExt.emit("theme", "light");
out.seen = seen;
console.log(JSON.stringify(out));
""",
    )
    assert result["seen"] == ["early:dark", "late:dark", "lang:de", "late:light", "late:light"]


def test_bridge_channel_may_appear_after_the_script_ran(tmp_path):
    result = run_node(
        tmp_path,
        BRIDGE_PRELUDE
        + """
load();
out.before = AppExt.close();
const sent = [];
globalThis.AppExtBridge = { postMessage: (m) => sent.push(m) };
out.after = AppExt.close();
out.sent = sent.length;
console.log(JSON.stringify(out));
""",
    )
    assert result["before"] is False and result["after"] is True and result["sent"] == 1


def test_bridge_takes_what_the_app_pushes_as_window_events_and_state(tmp_path):
    # The app (ui/lib/domain/extension_bridge.dart) dispatches a CustomEvent per change and keeps
    # window.AppExtState; a page that loads later must still see the latest values.
    result = run_node(
        tmp_path,
        BRIDGE_PRELUDE
        + """
const listeners = {};
globalThis.addEventListener = (name, fn) => { listeners[name] = fn; };
globalThis.AppExtState = { theme: "dark", language: "de" };
load();
const seen = [];
AppExt.onTheme((t) => seen.push("theme:" + t));
AppExt.onLanguage((l) => seen.push("language:" + l));
listeners["appext:theme"]({ detail: "light" });
listeners["appext:language"]({ detail: "en" });
listeners["appext:theme"]({ detail: 5 });                 // ignored: not a string
out.seen = seen;
console.log(JSON.stringify(out));
""",
    )
    assert result["seen"] == ["theme:dark", "language:de", "theme:light", "language:en"]


# -- in the FMIS web app: an iframe that talks to its parent ------------------------------------------------

FRAME_PRELUDE = """
import { readFileSync } from "node:fs";
const out = {};
const code = readFileSync(new URL("./bridge.js", import.meta.url), "utf8");
const listeners = {};
const posted = [];
const parent = { postMessage: (text, origin) => posted.push({ message: JSON.parse(text), origin }) };
const document_ = (() => {
  const handlers = {};
  const make = (tag) => ({ tag, style: {}, children: [], attrs: {}, listeners: {}, textContent: "",
    setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k] ?? null; },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(n, f) { this.listeners[n] = f; },
    attachShadow() { this.shadow = make("#shadow"); return this.shadow; } });
  const body = make("body");
  body.insertBefore = (el) => { body.children.unshift(el); };
  return {
    readyState: "complete", documentElement: { lang: "en" }, title: "Hello", body,
    createElement: make, createElementNS: (ns, tag) => make(tag),
    getElementById: () => null, querySelector: () => null,
    addEventListener: (n, f) => { handlers[n] = f; },
  };
})();
function load({ appOrigins, framed, ua = "Mozilla/5.0", opener = null, extra = {} } = {}) {
  globalThis.window = globalThis;
  globalThis.__APPEXT_CONFIG__ = { appOrigins, name: "Reports", nameLocalized: { de: "Berichte" } };
  globalThis.parent = framed ? parent : globalThis;
  globalThis.document = document_;
  globalThis.opener = opener;
  Object.defineProperty(globalThis, "navigator", { value: { userAgent: ua, language: "en" }, configurable: true });
  globalThis.addEventListener = (name, fn) => { listeners[name] = fn; };
  globalThis.location = { assign: (u) => (out.assigned = u) };
  globalThis.CustomEvent = class { constructor(type, init) { this.type = type; this.detail = init.detail; } };
  globalThis.dispatchEvent = (event) => listeners[event.type]?.(event);
  Object.assign(globalThis, extra);
  (0, eval)(code);
}
const WEB = "https://app.fmis.test";
"""


def test_the_frame_tells_its_parent_that_it_is_up_and_only_that_parent(tmp_path):
    result = run_node(
        tmp_path,
        FRAME_PRELUDE
        + """
load({ appOrigins: [WEB, "https://staging.fmis.test"], framed: true });
out.inApp = AppExt.inApp;
out.posted = posted.slice();
posted.length = 0;
AppExt.setTitle("Reports");
out.title = posted.slice();
console.log(JSON.stringify(out));
""",
    )
    assert result["inApp"] is True
    # `ready` goes to each configured origin – never to "*" – and the browser delivers it to the one that is the parent.
    assert [(p["message"], p["origin"]) for p in result["posted"]] == [
        ({"command": "ready"}, "https://app.fmis.test"),
        ({"command": "ready"}, "https://staging.fmis.test"),
    ]
    assert {p["origin"] for p in result["title"]} == {"https://app.fmis.test", "https://staging.fmis.test"}


def test_the_frame_takes_theme_and_language_only_from_its_parent(tmp_path):
    result = run_node(
        tmp_path,
        FRAME_PRELUDE
        + """
load({ appOrigins: [WEB], framed: true });
const seen = [];
AppExt.onTheme((t) => seen.push("theme:" + t));
AppExt.onLanguage((l) => seen.push("language:" + l));
const send = (source, origin, data) => listeners.message({ source, origin, data: typeof data === "string" ? data : JSON.stringify(data) });

send(parent, WEB, { appext: "event", type: "theme", value: "dark" });          // the parent, the web app: yes
send(parent, WEB, { appext: "event", type: "language", value: "de" });
send(parent, "https://evil.test", { appext: "event", type: "theme", value: "light" });   // right frame, wrong origin
send({}, WEB, { appext: "event", type: "theme", value: "light" });             // right origin, not the parent
send(parent, WEB, "not json");
send(parent, WEB, { appext: "event", type: "token", value: "x" });             // unknown type
send(parent, WEB, { appext: "event", type: "theme", value: 5 });               // not a string
out.seen = seen;
out.state = globalThis.AppExtState;
console.log(JSON.stringify(out));
""",
    )
    assert result["seen"] == ["theme:dark", "language:de"]
    assert result["state"] == {"theme": "dark", "language": "de"}


def test_a_page_in_the_phone_app_or_without_a_web_app_does_not_talk_to_any_parent(tmp_path):
    result = run_node(
        tmp_path,
        FRAME_PRELUDE
        + """
load({ appOrigins: [], framed: true });      // framed by something, but no web app is configured
out.noWebApp = [AppExt.inApp, AppExt.close()];
console.log(JSON.stringify({ ...out, posted }));
""",
    )
    assert result["noWebApp"] == [False, False] and result["posted"] == []


# -- in a plain browser tab: the way back -------------------------------------------------------------------


def test_a_plain_tab_gets_a_bar_with_the_way_back_to_the_web_app(tmp_path):
    result = run_node(
        tmp_path,
        FRAME_PRELUDE
        + """
load({ appOrigins: [WEB], framed: false });
const host = document_.body.children[0];
const bar = host.shadow.children[0];
const [button, title, fmis] = bar.children;
out.id = host.id;
out.title = title.textContent;
out.label = button.attrs["aria-label"];
out.sticky = host.style.cssText.includes("position:sticky");
button.listeners.click();
out.assigned = out.assigned;
console.log(JSON.stringify(out));
""",
    )
    assert result["id"] == "appext-shell" and result["title"] == "Reports" and result["label"] == "Back to FMIS"
    assert result["sticky"] is True
    assert result["assigned"] == "https://app.fmis.test/"


def test_the_bar_closes_a_tab_the_web_app_opened_instead_of_loading_the_app_again(tmp_path):
    result = run_node(
        tmp_path,
        FRAME_PRELUDE
        + """
let closed = 0;
load({ appOrigins: [WEB], framed: false, extra: { opener: {}, close: () => closed++ } });
document_.body.children[0].shadow.children[0].children[0].listeners.click();
console.log(JSON.stringify({ closed, assigned: out.assigned ?? null }));
""",
    )
    assert result == {"closed": 1, "assigned": None}


@pytest.mark.parametrize(
    "setup",
    [
        'globalThis.AppExtBridge = { postMessage() {} }; load({ appOrigins: [WEB], framed: false });',  # the phone app's WebView (the channel)
        'load({ appOrigins: [WEB], framed: false, ua: "Mozilla/5.0 FMIS-App-WebView/1" });',            # the marker alone: the channel is not there yet
        'load({ appOrigins: [], framed: false });',                                                     # no web app: nothing to lead back to
        'document_.querySelector = () => ({ getAttribute: () => "off" }); load({ appOrigins: [WEB], framed: false });',  # <meta name="appext-shell" content="off">
        'load({ appOrigins: [WEB], framed: true });',                                                   # inside the web app's frame: the app has its own bar
    ],
    ids=["phone-app", "phone-app-marker", "no-web-app", "page-opts-out", "framed"],
)
def test_there_is_no_bar_where_the_app_or_the_page_brings_its_own_navigation(tmp_path, setup):
    result = run_node(
        tmp_path,
        FRAME_PRELUDE + setup + "\nconsole.log(JSON.stringify({ bars: document_.body.children.length }));\n",
    )
    assert result["bars"] == 0
