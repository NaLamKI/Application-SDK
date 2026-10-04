# The host app

This document is for the developers of an app – on a phone, on the desktop or in the web – that shows a platform's extensions: what it must read from the store, how it opens an extension without a second password prompt, what it may let a page ask of it, and what it must never do.

A host app is **optional**. Without one, every extension is an ordinary website: it signs people in with its own redirect URI in any browser, and `APPEXT_APP_REDIRECT_URI` and `APPEXT_APP_ORIGINS` are simply not set ([auth-bundle.md](auth-bundle.md)). What a host app adds is the silent sign-in (the person's session at the identity service is reused; no password prompt), the app's own navigation around the page, and a small bridge.

Everything below was cross-checked against `/_sdk/bridge.js` of the SDK (`src/appext/js/bridge.js`) and against the reference host app. Statements marked "reference implementation" describe a choice it made, not part of the contract. MUST, SHOULD and MAY are used as in RFC 2119.

## 1. Four ways to show an extension

| Surface | How | Sign-in | Bridge |
|---|---|---|---|
| **Phone or tablet app** | a WebView inside the app | **silent hand-over** to the system browser's session (§3) | JavaScript channel `AppExtBridge` (§5) |
| **Web app** | an `<iframe>` below the app's own header (§6) | the extension signs in inside the frame with the identity service's session the browser already has | `postMessage` (§6) |
| **Desktop app without a WebView** | the system browser | the extension signs in like any website | none; the tab shows a way back (§6.4) |
| **`display = "external"`**, and every **link** | the system browser (a new tab on the web) | the extension signs in like any website; a link has no sign-in at all (§7) | none |

## 2. What the host app reads

The host app is a client of the catalog API ([app-store-api.md](app-store-api.md) §12): `GET /catalog` with `If-None-Match`, and `PUT` / `DELETE /extensions/{id}/installation`. It sends **its own** access token.

**What it may open is what stood in the last catalog it loaded successfully** – nothing else: no address from a link or a deep link, no object kept from earlier. A suspended extension disappears at the next successful load.

**Reading an entry.** A host app MUST NOT fail the whole catalog because of one entry: it drops entries it cannot use and keeps the rest. It drops an entry when:

| Entry | Dropped when |
|---|---|
| any | `entryUrl` is not an `https` address (`http` is accepted for `127.0.0.1`, `localhost`, `::1` only) or has no host; the entry's host is not one of its own `hosts` (lower-case); `minAppVersion` is present but not a SemVer string; a second entry with an already seen `id` (the first wins) |
| `kind` other than `link` | `callbackScheme` is not **the scheme this app serves** (§4); `clientId` is missing or empty; `callbackUrl` is missing, or not an `https` address (`http` for loopback) on one of its own `hosts` |
| `kind = "link"` | `entryUrl` carries user info; the entry has an `iconUrl` that is not on the host of `entryUrl` (a link has no `callbackScheme`, `callbackUrl` or `clientId` and asks for nothing: it ignores `scopes` and `display`) |

Further rules:

- `kind` or `display` absent or **unknown** is read as `extension` / `in_app`; a link is always external. New values can therefore be added to the catalog without breaking older apps. An app that predates links drops them (it requires `callbackScheme`) rather than opening them wrongly.
- **Icons** (`iconUrl`) are fetched by the app on its own, before anyone opens anything – so only from the entry's own `hosts`, without token, cookie or redirect, and with a size limit (the reference implementation: 256 KB). A bad icon gets the app's fallback icon; it is no reason to hide the extension. An extension serves its icon, SVG or raster, at `/_sdk/icon` with `Access-Control-Allow-Origin: *` (the web app reads it from another origin); an SVG is served with a policy that forbids script in it.
- **The identity provider is never taken from the catalog.** The catalog names no issuer. The app signs in at its own, and uses **that** address wherever §3 says "the identity provider". An address from the catalog could steer an extension's sign-in to an arbitrary provider, and the app would pass the authorization request on to it.
- The envelope's `redirectUri` is **confirmation only**; the app builds its own request from the scheme it serves.
- Role filtering and `installed` are decided by the server per person; the app cannot be trusted to hide an entry.
- Show **what the extension will ask for** (the `scopes[]` texts) before the first opening. The binding consent is the one at the identity service.
- The extension's title and description come in English with translations (`titleLocalized`, …): show the one that fits the app's language, else English.

## 3. Opening an extension in a WebView

```
WebView                    App                      System browser            Identity service
  │ loads the entry         │                             │                          │
  │ (marker in user agent,  │                             │                          │
  │  cookie app_sub)        │                             │                          │
  │── 302 → authorize ─────▶│ navigation delegate:        │                          │
  │   redirect_uri=         │ the extension's OWN request │                          │
  │   <scheme>:/callback    │ (client, redirect, code)    │                          │
  │                         │── authentication session ──▶│── authorize ────────────▶│
  │                         │                             │   session cookie valid,  │
  │                         │                             │   consent (first time)   │
  │                         │◀─ <scheme>:/callback?code=…&state=…&iss=… ─────────────│
  │◀─ loads <callbackUrl>?<the same parameters, unchanged> ────────────────────────  │
  │ the extension's backend redeems the code (credentials + verifier the app never sees)
```

**Invariants** everything else follows from:

| Invariant | How it is kept |
|---|---|
| No token of the app leaves the app | the WebView never carries one; the app only passes the callback URL through |
| Only the extension's backend redeems the code | the extension's client is confidential; an intercepted code is useless without its credentials and the PKCE verifier |
| The identity provider never loads in the WebView | every navigation is decided by the rules below; a form there would be a password prompt in a context the app could eavesdrop on |
| The app delegates only the extension's **own** request | the whitelist below, with a negative test per criterion |
| The extension's token can do only what its scopes say | the extension's client carries only the approved scopes ([oauth-service.md](oauth-service.md) §12) |

### 3.1 The steps

1. **Tap.** If the entry opens externally (§7), hand `entryUrl` to the system browser *as the first thing the tap does*, and stop.
2. **Renew first.** Refresh the app's own session at the identity service before anything opens. The silent hand-over finds exactly this session in the system browser; a renewal extends its idle time, and without it the session could expire while the person is opening the screen and a sign-in form would appear instead of the extension. If renewal fails (no network, session expired), **open nothing** and say why.
3. **Prepare, in this order, before the first request:**
   - (a) Append the marker to the WebView's user agent: ` <Name>-App-WebView/<version>` (§4).
   - (b) Set the cookie **`app_sub`** = the `sub` of the signed-in account, `domain` = the entry's host, `path` = `/`. The extension compares it with the `sub` of its own ID token: if someone else is signed in in the system browser than in the app, it throws the tokens away and restarts with `prompt=login`.
   - (c) Load `entryUrl`.

   If (a) or (b) fails, **do not open**: without the cookie the extension could not notice a foreign identity at the system browser. Fail closed. The marker is not a security feature (a forged marker only yields a return address the forger's browser cannot open); the cookie is not a secret (tampering only leads to a rejection) – but both must be in place for the extension to behave.
4. **Decide every navigation** with the rules of §3.2.
5. **Hand over** the extension's own authorize request (§3.3), then load the return URL in the same WebView.
6. After every page load on an own page, **push theme and language** into it (§5).

### 3.2 Navigation rules

Evaluated top to bottom for every navigation request of the WebView. "The identity provider" is the origin (host **and** port) of the app's own issuer.

| # | Target | Decision |
|---|---|---|
| 1 | an address on the identity provider | the extension's **own** authorize request (below) → **hand over**; anything else → **block** |
| 2 | scheme `about` (for example `about:blank`) | allow |
| 3 | a scheme other than `https` – `http` only for `127.0.0.1`, `localhost`, `::1` | **block** (`javascript:`, `intent:`, `tel:`, the app's own scheme, …) |
| 4 | a host in the entry's `hosts` | allow |
| 5 | any other host | **block** |

Rule 5 blocks; it does not "open it in the browser instead". A page that redirects to a foreign address without the person's doing would otherwise steer the operating system's browser to an arbitrary address – out of the app, with the trust its name brings. A deliberate way out is the bridge's `openExternal` (§5), which the app judges by host rules of its own.

**The extension's own authorize request** – all four must hold:

| Criterion | Value |
|---|---|
| address | the issuer's authorization endpoint (the reference implementation compares the path `<issuer path>/protocol/openid-connect/auth`, which is **Keycloak's**; an app for another provider should compare against the `authorization_endpoint` of its **own** issuer's discovery document) |
| `client_id` | the entry's `clientId` |
| `redirect_uri` | exactly `<scheme>:/callback` (§4) |
| `response_type` | `code` |

Anything else on the identity provider – a request for another client, the app's own, a consent screen for a foreign client – is blocked: a compromised page could otherwise ask for consent for a foreign client in the familiar system sheet.

**A second net.** On Android the WebView does not report every redirect before loading it. When a page *has started* on the identity provider anyway, the app replaces it with `about:blank` **immediately** – before anyone sees a form – and hands over if it was the extension's own request.

**Own page.** Narrower than "allowed": a page on the host of the entry or of the callback address, over `https` (`http` for loopback), and not the identity provider. The auxiliary `hosts` (images, fonts) may load into the WebView but are never *the page being looked at*. The bridge's commands (§5) and the pushing of events are accepted only from an own page.

### 3.3 The hand-over

- Open the authorize URL **exactly as the page requested it** in the platform's **authentication session** (`ASWebAuthenticationSession` on iOS, Custom Tabs on Android) with the callback scheme of §4, and **non-ephemeral** – a shared session: only that finds the single sign-on cookie of the app's own sign-in. A private session would ask for the password for every extension.
- **At most one hand-over at a time**: a page that redirects twice must not open two sheets on top of each other.
- When the session returns a URL, check that its **scheme** is the app's and its **path** is `/callback`. Then load the entry's `callbackUrl` with the **query parameters of the response unchanged** (`code`, `state`, `iss`, `error`, … – the app does not know their meaning and must not need to). Anything else counts as a cancellation.
- On **cancellation** (the sheet is closed, no browser, a platform exception) load `callbackUrl?error=access_denied&state=<the state of the request>`. The extension treats it as a refusal, cleans up its transaction and shows a message instead of loading forever.
- The app **never reads, stores or logs** the `code`.

### 3.4 What a device taught the reference implementation

- A navigation the app stops itself is reported by the WebView as a load **error** a little later, and for as long as the sheet is open. Ignore main-frame errors while a hand-over runs and for a short moment (the reference: 5 s) after the app cancelled a navigation; otherwise an error screen covers a page that loaded fine after the hand-over. Only a failure of the **main** frame counts as "unreachable"; a failing image is not.
- Until a page of the extension has loaded, show **the app's own screen** (icon, name, "Opening …" / "Signing in …") over the WebView instead of a blank page and the plumbing of redirects (reference implementation, a product decision).
- **Back** goes through the extension's pages first and leaves the extension at its start.
- **Add = sign in once** (reference implementation, a product decision): after `PUT …/installation` the app opens the extension behind its own cover, lets the sign-in and consent run, and closes as soon as the first page of the extension has loaded, so that opening it later is instant. A sign-in that fails (cancelled, refused, extension unreachable) still leaves the app added; the person is told the confirmation will come at the first opening. The sign-in's own routes (`/auth/…`) ending in an error page mean it did *not* come through.
- **Sessions survive restarts of the extension** only if the extension keeps them outside its process (a Redis session store); otherwise every restart makes the person see the hand-over again.

### 3.5 Sign-out

When the person signs out of the app, or deletes the account, the app MUST **clear the WebView's cookies**: the next person on the device would otherwise find the extension's session even if the identity service signed out long ago. The identity service's back-channel logout reaches the extensions too ([oauth-service.md](oauth-service.md) §9).

## 4. The marker and the app redirect URI

**The marker.** The host app appends **`<Name>-App-WebView/<version>`** to the user agent of its WebView (for example `Example-App-WebView/1`), after the platform's own user agent and one space. The SDK looks for the substring **`-App-WebView/`**; a marker that does not contain it is configured with `APPEXT_APP_MARKER`, which replaces the part looked for ([auth-bundle.md](auth-bundle.md) §3.2). The version is not interpreted. Finding the marker – and only if `APPEXT_APP_REDIRECT_URI` is set – makes **that sign-in transaction** use the app's redirect URI instead of `<public URL>/auth/callback`. Nothing else in the extension changes.

**The redirect URI** is `<scheme>:/callback` – one slash, the path `/callback` – for example `com.example.app:/callback`, **the same for every extension** of the platform. Requirements on the scheme:

| Requirement | Why |
|---|---|
| it is **registered with the operating system for the app** (Android: an activity with an intent filter for `VIEW`, categories `DEFAULT` and `BROWSABLE`, and the scheme; iOS: the scheme is a parameter of the authentication session – the reference implementation registers nothing for it in the iOS app) | the return of the authentication session has to arrive in the app |
| it is a **private-use scheme the platform owns**, reverse-domain style (RFC 8252 §7.1) | a scheme another app can claim lets that app intercept the code (RFC 8252 §8.6). The code is worthless without the extension's credentials and verifier, but a design should not rest on that alone |
| it **differs from the scheme of the app's own sign-in** | on Android the redirect activity of the app's sign-in library and the one of the hand-over would claim the same scheme, and the response would arrive in the wrong one. The reference implementation's store refuses to start if they are equal. Example: the app signs in with `com.example.app.login:/oauth2redirect` and hands extensions over with `com.example.app:/callback` |
| it is **built into the app**, not read from the catalog | the scheme is in the app's manifest, thus in the program; an entry with another `callbackScheme` is not offered – it could be opened and never return |
| it is a **registered redirect URI of every extension client** at the identity service | the store's provisioning adds it ([oauth-service.md](oauth-service.md) §3.5) |

The app's **own** sign-in MUST also run in the shared system browser session (for example `prefersEphemeralWebBrowserSession: false`): the hand-over finds its single sign-on cookie there.

## 5. The JavaScript bridge (phone and tablet)

The WebView gets a JavaScript channel named **`AppExtBridge`**. The page (through `/_sdk/bridge.js`, which every extension serves and loads into every HTML page the SDK serves) sends **fixed commands**; the app **never answers** (a channel has no return value). In the other direction the app pushes **two events**. There is **no data** in either direction: no command returns a token, a user name or a record – data reaches the page through the extension's own backend.

### 5.1 Page → app

`AppExtBridge.postMessage(JSON.stringify({command, …}))`:

| Command | Message | The app | Validation |
|---|---|---|---|
| `close` | `{"command": "close"}` | closes the extension screen | – |
| `setTitle` | `{"command": "setTitle", "title": "Reports"}` | sets the title in the app bar above the page | a string; control characters become spaces, runs of whitespace collapse, trimmed; empty → rejected; cut to **60 characters** |
| `openExternal` | `{"command": "openExternal", "url": "https://projects.apps.example.com/export.pdf"}` | opens the address in the **system browser** | `https` only, **no user info**, and the host must pass the navigation rules of §3.2 (so: one of the extension's own `hosts`, never the identity provider) |
| `ready` | `{"command": "ready"}` | the phone app ignores it (only the web app's frame has a use for it, §6) | – |

Rules for all of them:

- The message is at most **4096 characters**, valid JSON, an object whose `command` is a string from the fixed set. **Unknown commands are ignored**, without an answer the page could learn from.
- Accepted **only while the WebView's *current* address is an own page** (§3.2) – asked of the WebView at that moment, not remembered. A channel reaches every frame, so this is the check that keeps a foreign frame out; an extension that navigates elsewhere loses the bridge.
- `openExternal` is deliberately **narrower than the SDK's documentation suggests**: `AppExt.openExternal(url)` is described as opening an `https` address, but a page that could send the system browser to any address – with the trust the app's name brings – is exactly what the navigation rules prevent. The reference implementation therefore opens only the extension's own hosts. An extension author cannot rely on arbitrary links opening; a platform that wants to allow more MAY, but SHOULD NOT allow `http`, credentials, the identity provider or a non-`https` scheme.

### 5.2 App → page

The app runs this script in the WebView, **after every page load on an own page** (a new page knows nothing of what the last one was told) and on every change of theme or language; name and value are encoded as JSON string literals, never pasted in:

```js
(function(n,v){var s=window.AppExtState=window.AppExtState||{};s[n]=v;
 window.dispatchEvent(new CustomEvent('appext:'+n,{detail:v}));})("theme","dark");
```

| Event (on `window`) | `detail` | Also in |
|---|---|---|
| `appext:theme` | `"light"` or `"dark"` | `window.AppExtState.theme` |
| `appext:language` | the app's language code: `"de"`, `"en"`, … (the reference implementation sends two or three lower-case letters, `^[a-z]{2,3}$`) | `window.AppExtState.language` |

`window.AppExtState` holds the latest values for scripts that load after the event fired; `bridge.js` reads it at start and replays it to its subscribers (`AppExt.onTheme`, `AppExt.onLanguage`).

## 6. The web app

### 6.1 Embedding

The web app shows the extension in an **`<iframe src="<entryUrl>">` below its own header**: the app's navigation and back button stay around it. There is **no hand-over**: the browser signs in inside the frame with the identity service's session it already has; the consent screen appears there too. The same bridge, a different transport.

The frame is **sandboxed**:

```
sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals"
```

**without `allow-top-navigation`** (the page cannot navigate the app away from under the person). This is what makes up for a limit of frames: a WebView can veto every navigation, a frame cannot. A frame cannot be held to the extension's own hosts; it can be kept from taking the app along, and its commands count only from the extension's origin.

### 6.2 Transport

| Direction | Mechanism |
|---|---|
| page → app | `parent.postMessage(<JSON text>, <app origin>)`, once for **each** origin in `APPEXT_APP_ORIGINS`, **never** to `*`. The JSON text is the same as in §5.1 |
| app → page | `frame.contentWindow.postMessage('{"appext":"event","type":"theme","value":"dark"}', <extension origin>)` – `type` is `theme` or `language`, `value` a string, the same values as §5.2. `bridge.js` accepts it only from `window.parent`, only from an origin in `APPEXT_APP_ORIGINS`, and turns it into the same window event and `AppExtState` entry as on the phone |

The app listens to `message` events and takes one **only if `event.source` is this frame's window**, then judges the **origin the browser reports** (not anything the page claims) by the own-page rule of §3.2. Messages from any other window are ignored.

**`ready`.** After the page has loaded, `bridge.js` sends `{"command": "ready"}`. The app answers by sending theme and language, and learns from it that the frame is the extension's page and not the browser's own error page for a refused frame (an error page cannot speak). The identity service's login and consent pages cannot say `ready` either; they must remain usable. The reference implementation therefore:

- takes its cover away after **3 s** whatever is in the frame;
- after **10 s without `ready`** shows a slim bar offering "Reload" and "Open in browser" – and leaves the frame where it is, because someone may be in the middle of signing in.

### 6.3 What must be true for a frame to work

| # | Requirement | Where it is configured |
|---|---|---|
| 1 | the **extension** allows exactly the web app as a frame: `frame-ancestors <origin>` instead of `'none'`, and no `X-Frame-Options` | `APPEXT_APP_ORIGINS` in the bundle; the SDK builds the policy. Not for `display = "external"` |
| 2 | the **identity service's** login and consent pages may be framed by the **same origin** (and nobody else) | its browser security headers: `Content-Security-Policy: frame-ancestors 'self' <web app origin>`, `X-Frame-Options` off. A wildcard here is a clickjacking hole; who may frame the login page is a security decision |
| 3 | web app, extensions and identity service share **one registrable domain** (`app.example.com`, `<id>.apps.example.com`, `auth.example.com`) | DNS and certificates. Extension cookies are `SameSite=Lax`; browsers block third-party cookies in frames (Safari always), so the sign-in inside the frame fails across sites |
| 4 | the web app itself does **not** set `Cross-Origin-Embedder-Policy: require-corp` | it would demand an opt-in header from every framed document, the identity service's included |

The origins in `APPEXT_APP_ORIGINS` are validated at start: an origin, nothing else (`https`, `http` only for loopback, no wildcard, no `;`), because each one ends up in every extension's Content-Security-Policy. For local development use the **IP literal** (`http://127.0.0.1:8088`, not `localhost`): cookies are scoped by host, and a frame on `localhost` would not share them with an extension on `127.0.0.1`.

### 6.4 In a browser tab

An extension opened **outside** the app – in a new tab, from "open in browser", or because it is `display = "external"` – gets a **bar** from `bridge.js`: a back arrow, the extension's name (translated by the page's language) and the platform's name as a badge. It appears only when `APPEXT_APP_ORIGINS` is set **and** the page is neither in the phone app's WebView nor in a frame. The arrow closes the tab if it was opened by script (`window.opener`), otherwise it navigates to **the first origin of `APPEXT_APP_ORIGINS`**, root path – so the web app MUST answer at `<origin>/`. The label is `Back to <APPEXT_APP_NAME>` unless `APPEXT_APP_BACK_LABELS` translates it; `APPEXT_APP_ACCENT` colours it ([auth-bundle.md](auth-bundle.md) §3.2). A page with its own navigation switches the bar off with `<meta name="appext-shell" content="off">`.

## 7. External display and links

**`display = "external"`.** The manifest says the extension wants to be a point of departure rather than shown inside the app. The host app hands `entryUrl` to the **system browser** (the web build: a new tab) and does **nothing else**:

- no renewal (no hand-over follows), no WebView, no frame, **no sign-in when it is added** – the page signs in like any website, as an OpenID Connect client, and the consent happens there;
- the **host binding of the WebView does not apply**: the page may send the person wherever it wants, also to the provider's own website or, through a universal link, into the provider's own app;
- there is no bridge; the page's bar leads back (§6.4). The SDK answers such an extension with `frame-ancestors 'none'` even where `APPEXT_APP_ORIGINS` names a web app: nothing frames it;
- the catalog's detail view and the tile say so **beforehand** ("opens in your browser", a small external-link mark);
- the launch of the browser is the **first thing** the tap does: on the web, a launch that comes after an `await` is blocked as a pop-up.

An entry of **`kind = "link"`** takes the same path as far as the host app is concerned – it is external by nature – and additionally:

- **nothing is passed on**: no token, no name, no account, no `app_sub`;
- the detail view says what a link is ("Points to <host>. This opens a page of the provider in your browser; nothing of this app is passed on.") and asks for no permissions;
- the app reads it by its own rules (§2) and the navigation rules of §3.2 do not exist for it.

## 8. What the host app MUST NOT do

1. **Pass a token** – access, refresh or ID token, or anything derived from one – to the page, the WebView, the extension or the bridge: not in an address, a cookie, a header, a script or a message. The only thing that identifies the account is the `sub` in the cookie `app_sub`, and it is not a secret.
2. **Evaluate, store or log the `code`** of a hand-over. It forwards the response unchanged.
3. **Load the identity provider in the WebView**, not even "just the sign-in page".
4. **Take the identity provider, the authorization endpoint, the scheme or the redirect URI from the catalog.**
5. **Hand over any authorize request but the extension's own** (all four criteria of §3.2).
6. **Let a page send the WebView or the system browser to a foreign host** by itself; block, do not "open in browser".
7. **Add data commands** to the bridge, **answer** commands, or accept commands from a page that is not an own page.
8. **Post to `*`** or to an origin other than the extension's (web), or accept messages from a window other than the frame.
9. **Open a WebView without the marker and the `app_sub` cookie.**
10. **Open an extension that was not in the last catalog it loaded successfully**, or show the cached catalog of another account (§9).
11. **Use a private (ephemeral) authentication session** for the hand-over or for the app's own sign-in.
12. **Use the app's own sign-in scheme** for the hand-over.

## 9. Versions and the offline catalog

**`minAppVersion`.** The host app has a version (SemVer, without a build number). An entry with a `minAppVersion` is shown only if the app's version is **greater than or equal** to it. If either version cannot be read the entry is **hidden**: showing an extension that might not run is the worse mistake. The app's version is a constant of the build that a test keeps equal to the packaging metadata.

**The cache.** The app stores the **last successful catalog response**, as received, together with its `ETag` and **the `sub` of the account it belongs to**, in one record (two records could part company on a crash, and a `304` against a different body would show a wrong list as a right one).

- The request carries the stored `ETag`; `304` means "what you hold is current". A `304` for a request that carried **no** ETag is something the server was not asked for: ask once more, plainly.
- If the store **cannot be reached** (a network failure), the cached catalog is used and marked as such. Any **other** failure – the sign-in expired, the person may not – is **not** answered from the cache: the person needs to hear it, and a list the server has not confirmed would pass for one it has.
- The cache is **personal** (role filter, `installed`). If someone else signs in on the device, the stored catalog MUST NOT be shown to them and its ETag MUST NOT be sent: the server's `304` would hand them the other person's list. A catalog that cannot be attributed to an account is not shown to anyone.
- A cached record is parsed again, with the same strict reading as a fresh response, every time it is read.
- Adding and removing need the network and say so.
- Without an account (a purely local mode of the app) there is no catalog; the app says why.

## 10. Wire formats at a glance

| | Phone / tablet WebView | Web app frame |
|---|---|---|
| page → app | `AppExtBridge.postMessage('{"command":"close"}')` – also `setTitle`, `openExternal` | `parent.postMessage('{"command":"close"}', '<app origin>')` – also `setTitle`, `openExternal`, `ready` |
| app → page | script: sets `window.AppExtState[name]`, dispatches `CustomEvent('appext:'+name, {detail: value})` on `window` | `frame.contentWindow.postMessage('{"appext":"event","type":"theme","value":"dark"}', '<extension origin>')`; `bridge.js` turns it into the same window event |
| trust | current URL of the WebView is an own page | `event.source` is the frame's window; `event.origin` passes the own-page rule |
| limits | 4096 characters per message; title 60 characters | the same |

## 11. Conformance checklist

**Catalog**
- [ ] The app calls `GET /catalog` with its own token and `If-None-Match`; handles `200`, `304` and failures as in §9; drops broken entries individually.
- [ ] It opens only what stood in the last successful catalog, and drops entries with a foreign `callbackScheme`, a missing host, an unreadable `minAppVersion`.
- [ ] `PUT` / `DELETE` installation are used for adding and removing; the app shows the scope texts before the first opening.
- [ ] The cached catalog is bound to the account; an ETag of another account is never sent.

**Phone WebView**
- [ ] Before opening: renew the app's session; set the marker `<Name>-App-WebView/<version>` and the cookie `app_sub` (domain = entry host) **before** loading the entry; fail closed.
- [ ] The navigation rules of §3.2 are implemented in that order, plus the second net; the identity provider is the app's **own** issuer.
- [ ] The own-request whitelist has four criteria and a negative test for each.
- [ ] Hand-over: non-ephemeral session, one at a time, scheme and path checked, parameters passed on unchanged, `error=access_denied&state=…` on cancellation.
- [ ] A navigation the app cancelled itself does not raise the "unreachable" screen.
- [ ] WebView cookies are cleared on sign-out and account deletion.

**Scheme and marker**
- [ ] The app redirect URI is `<scheme>:/callback`, the scheme is registered with the OS, differs from the app's own sign-in scheme, is built into the app, and is a redirect URI of every extension client.
- [ ] The marker contains `-App-WebView/`, or `APPEXT_APP_MARKER` says what it is.
- [ ] The app's own sign-in uses the shared system browser session.

**Bridge**
- [ ] The channel is called `AppExtBridge`; commands `close`, `setTitle`, `openExternal`, `ready` only; unknown ones are ignored; no answers; ≤ 4096 characters; title cut to 60; `openExternal` is `https` without credentials on an allowed host.
- [ ] Commands are accepted only while the WebView is on an own page.
- [ ] `appext:theme` and `appext:language` are pushed after every page load and on every change; `window.AppExtState` is kept.

**Web app**
- [ ] The frame is sandboxed without `allow-top-navigation`; messages are taken only from the frame's window and judged by the browser-reported origin; events go only to the extension's origin.
- [ ] `ready` is answered with theme and language; a frame that stays silent offers reload and "open in browser" without being removed.
- [ ] Requirements 1–4 of §6.3 hold (CSP of the extension, framing rights of the identity service for this one origin, one registrable domain, no COEP).
- [ ] The web app answers at the root of every origin in `APPEXT_APP_ORIGINS`.

**External and links**
- [ ] `display = "external"` and links go to the system browser as the first action of the tap, with no renewal, no WebView, no frame, nothing passed on.

**Never**
- [ ] None of the twelve points of §8 is violated.
