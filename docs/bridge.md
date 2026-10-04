# The bridge to the host app

For developers of an extension's frontend, and for host-app developers who implement the other side of the bridge.

An extension runs in one of three places, and `/_sdk/bridge.js` (served by every extension)
is the one script for all of them. It wraps them in a small object, `AppExt`:

| Where | How it talks to the host app |
|---|---|
| The host app on a phone | a JavaScript channel named `AppExtBridge` in the WebView |
| The host's **web** app | the extension sits in an `<iframe>` under the app's own header; `postMessage` to the parent, and only to the web app's origin(s) (`APPEXT_APP_ORIGINS`) |
| An ordinary browser tab | nothing: every call is a quiet no-op that returns `false` – an extension must work here too |

The third row is also where an extension lands whose manifest says `display = "external"`
([manifest.md](manifest.md#display-in-the-app-or-in-the-browser)): the host app opens it in the system
browser instead of showing it, so there is no bridge to talk to. It is where an extension lands, too,
on a platform that has no host app at all.

**The SDK loads `bridge.js` into every HTML page it serves**, so nobody has to remember (a page that
includes it itself gets it once). Include it by hand only for pages your own code renders (HTMX
templates do: `base.html`).

```html
<script src="/_sdk/bridge.js"></script>
<script src="/app.js"></script>      <!-- no inline script: the CSP is default-src 'self' -->
```

```js
AppExt.setTitle("Reports");
const off = AppExt.onTheme((theme) => (document.documentElement.dataset.theme = theme));
AppExt.openExternal("https://example.com/help");
AppExt.close();
```

| Call | Effect |
|---|---|
| `AppExt.inApp` | `true` inside the host app – the phone app's WebView (the channel, or the marker in the user agent) or the web app's frame. |
| `AppExt.close()` | Closes the extension's screen. |
| `AppExt.setTitle(text)` | Sets the app bar title above the extension. |
| `AppExt.openExternal(url)` | Asks the host app to open an `https` URL in the system browser. The host app decides which addresses it accepts (it refuses anything but `https`, and a platform may narrow that further – see [the host app contract](platform-contract/host-app.md)). |
| `AppExt.onTheme(callback)` | Called with `"light"` or `"dark"` now (if known) and on every change; returns an unsubscribe function. |
| `AppExt.onLanguage(callback)` | Same, with the app's language code (`"en"`, `"fr"` …). |

## What the bridge deliberately does not do

**No data.** No command returns a token, a user name or anything else from the app. Data flows
through the extension's own backend, authenticated by the session. The app accepts exactly
the fixed commands above, ignores unknown ones, and takes them **only while the WebView is on
the extension's own host** (an extension that navigates elsewhere loses the bridge).

## What the SDK needs to know about the host app

The script cannot ask the host app anything, so the extension is told through its configuration
([deployment.md](deployment.md#configuration), [platform.md](platform.md)); the auth bundle of a
platform with a host app carries these:

| Variable | What the bridge does with it | Default |
|---|---|---|
| `APPEXT_APP_ORIGINS` | the origin(s) of the web app: the only ones it talks to and listens to in a frame, and the target of the bar's way back | empty: no web app, no bar |
| `APPEXT_APP_MARKER` | the part of the user agent that says "inside the phone app" | `-App-WebView/` |
| `APPEXT_APP_NAME` | the app's name in the bar and in the arrow's label | empty: "the app", and no name in the bar |
| `APPEXT_APP_BACK_LABELS` | the arrow's label per language, a JSON object; `{app}` stands for the name; keys are language codes (`en`, `fr`, `pt-BR`), see below | `Back to {app}` |
| `APPEXT_APP_ACCENT` | the colour of the arrow and of the name: `#` and 3 to 8 hex digits (`#rgb`, `#rrggbb`) | `#2563eb` |

## In a browser tab: the way back

Opened outside the app – in a tab, say from "open in browser" – the page gets a **bar on top**: a back
arrow, the extension's name and, at the right, the name of the host app. The arrow closes the tab if
the web app opened it, and otherwise leads to the web app (the first of `APPEXT_APP_ORIGINS`). Its
tooltip and accessible name are the label "Back to *name*", in the page's language when
`APPEXT_APP_BACK_LABELS` has one – the page language is the `lang` of `<html>`, else the browser's,
tried first as it is (`pt-BR`), then reduced to its language (`pt`); then the English entry, then the default. The bar appears
only when a web app is configured (`APPEXT_APP_ORIGINS`) and the page is neither in the phone app nor in a
frame. It is drawn in a shadow root with no inline styles (the CSP is `default-src 'self'`). A page with
its own navigation switches it off:

```html
<meta name="appext-shell" content="off">
```

`appext dev` does not set `APPEXT_APP_ORIGINS`: to see the bar at your desk, pass it with an
`--env-file` or the shell, for instance `APPEXT_APP_ORIGINS=http://localhost:3000 appext dev`, and open
the extension in a tab.

## Wire format

For anyone implementing the app side or testing without it:

```
extension -> app   AppExtBridge.postMessage(JSON.stringify({command: "close"}))        (phone app)
                   parent.postMessage(JSON.stringify({command: "close"}), appOrigin)    (web app, iframe)
                   {command: "setTitle", title: "…"}
                   {command: "openExternal", url: "https://…"}
                   {command: "ready"}        (iframe only: the page is up – the app answers with theme and language)
app -> extension   window event "appext:theme" (detail "light" | "dark"), "appext:language" (detail "en" …),
                   and window.AppExtState for a script that loads later          (phone app)
                   frame.contentWindow.postMessage('{"appext":"event","type":"theme","value":"dark"}', extensionOrigin)
                   – turned by bridge.js into the same window event              (web app, iframe)
```

In the web app a message counts only from the frame itself and only from an origin the page lists in
`APPEXT_APP_ORIGINS`; `bridge.js` never posts to `*`.

The bridge is optional: an extension that never loads `bridge.js` loses nothing but the title and
theme integration. What a host app has to implement is part of the platform contract
([platform-contract/README.md](platform-contract/README.md)).
