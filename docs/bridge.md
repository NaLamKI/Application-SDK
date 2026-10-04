# The bridge to the app

An extension runs in one of three places, and `/_sdk/bridge.js` (served by every extension)
is the one script for all of them. It wraps them in a small object, `AppExt`:

| Where | How it talks to the app |
|---|---|
| The FMIS app on a phone | a JavaScript channel named `AppExtBridge` in the WebView |
| The FMIS **web** app | the extension sits in an `<iframe>` under the app's own header; `postMessage` to the parent, and only to the web app's origin(s) (`APPEXT_APP_ORIGINS`) |
| An ordinary browser tab | nothing: every call is a quiet no-op that returns `false` – an extension must work here too |

The third row is also where an extension lands whose manifest says `display = "external"`
([manifest.md](manifest.md#display-in-the-app-or-in-the-browser)): the app opens it in the system browser
instead of showing it, so there is no bridge to talk to.

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
| `AppExt.inApp` | `true` inside the FMIS app – the phone app's WebView or the web app's frame. |
| `AppExt.close()` | Closes the extension's screen. |
| `AppExt.setTitle(text)` | Sets the app bar title above the extension. |
| `AppExt.openExternal(url)` | Opens an `https` URL in the system browser. The app refuses anything else. |
| `AppExt.onTheme(callback)` | Called with `"light"` or `"dark"` now (if known) and on every change; returns an unsubscribe function. |
| `AppExt.onLanguage(callback)` | Same, with the app's language code (`"de"`, `"en"`). |

## What the bridge deliberately does not do

**No data.** No command returns a token, a user name or anything else from the app. Data flows
through the extension's own backend, authenticated by the session. The app accepts exactly
the fixed commands above, ignores unknown ones, and takes them **only while the WebView is on
the extension's own host** (an extension that navigates elsewhere loses the bridge).

## In a browser tab: the way back

Opened outside the app – in a tab, say from "open in browser" – the page gets a **bar on top**: a back
arrow, the extension's name and "FMIS". The arrow closes the tab if the web app opened it, and otherwise
leads to the web app (the first of `APPEXT_APP_ORIGINS`). The bar appears only when a web app is
configured and the page is neither in the phone app nor in a frame. It is drawn in a shadow root with
no inline styles (the CSP is `default-src 'self'`). A page with its own navigation switches it off:

```html
<meta name="appext-shell" content="off">
```

## Wire format

For anyone implementing the app side or testing without it:

```
extension -> app   AppExtBridge.postMessage(JSON.stringify({command: "close"}))        (phone app)
                   parent.postMessage(JSON.stringify({command: "close"}), appOrigin)    (web app, iframe)
                   {command: "setTitle", title: "…"}
                   {command: "openExternal", url: "https://…"}
                   {command: "ready"}        (iframe only: the page is up – the app answers with theme and language)
app -> extension   window event "appext:theme" (detail "light" | "dark"), "appext:language" (detail "de" …),
                   and window.AppExtState for a script that loads later          (phone app)
                   frame.contentWindow.postMessage('{"appext":"event","type":"theme","value":"dark"}', extensionOrigin)
                   – turned by bridge.js into the same window event              (web app, iframe)
```

In the web app a message counts only from the frame itself and only from an origin the page lists in
`APPEXT_APP_ORIGINS`; `bridge.js` never posts to `*`.

The bridge is optional (phase 3 of the concept): an extension that never loads `bridge.js`
loses nothing but the title and theme integration.
