# Security

For reviewers, platform operators and developers who want to know what the SDK protects against and what remains theirs to do.

The SDK applies protective defaults that an extension does not need to switch off in order to
work. This table lists each measure and where it lives.

| Risk | Measure | In the SDK |
|---|---|---|
| An extension uses another's session | One subdomain per extension; the cookie has the `__Host-` prefix and no `Domain` attribute | cookie `__Host-ext_session`, `Path=/`, HttpOnly, Secure, SameSite=Lax |
| CSRF on `/api` | SameSite=Lax, a custom header on writing methods, `Origin` check | `X-Appext-CSRF` required; `Origin` must be the own origin; pages check `Origin` / `Sec-Fetch-Site` |
| XSS in the frontend | `default-src 'self'`, no inline scripts; tokens are not in the browser anyway | the default CSP (`appext new` templates need no inline anything); `X-Content-Type-Options: nosniff` |
| Embedding in foreign pages | `frame-ancestors 'none'` – except for the host app's web app, named in `APPEXT_APP_ORIGINS` (then only that origin; `X-Frame-Options` cannot name one, so it is left out) | in the CSP; the origins are validated at start (an origin, no wildcard, no `;`) and `bridge.js` posts only to them, never to `*` |
| Open redirect via `return_to` | Relative paths on the own origin only | `safe_return_to`: one leading `/`, no `//`, backslash, control character, scheme; never into `/auth` |
| Tokens too broad for a target | Token exchange per service with exactly its scopes; the login token stays in the backend | `ext.service()`, cache per session+audience+scopes |
| Leaking the session database | Tokens encrypted at rest, key outside the store | AES-GCM, `APPEXT_SESSION_KEY_FILE`, rotation by key list |
| Secrets in the image | Run-time secrets only; a scan in CI | `appext manifest check --scan-secrets`; the templates' `.dockerignore` |
| A compromised extension | Limited to granted scopes and consenting people; disabling the client at the issuer stops refresh and exchange; short access tokens | store `suspend`; a target service can also refuse an extension by its `azp` (`require_azp`) |
| A deployment talking to the wrong platform | Nothing is guessed: no built-in issuer, store or service URL; outside `local` no platform file is read and every setting must be present | `ExtensionSettings.from_env`; [platform.md](platform.md#in-production-the-same-values-as-variables) |

## More of what the SDK does

- **Login CSRF:** `state` is bound to a transaction cookie (HttpOnly, SameSite=Lax) and used
  once. **PKCE S256** on every sign-in; `nonce` in the ID token; `iss`, `aud`, `exp`, `sub`
  checked. A new sign-in never inherits a session id.
- **No token logging:** errors carry the identity provider's `error` code and description,
  never a token. `Secret` values are masked in any `repr`.
- **Account match:** the host app's `app_sub` cookie against the ID token's `sub`; a mismatch
  discards the tokens and restarts with `prompt=login`.
- **Refresh under a lock** (see [authentication.md](authentication.md)); a session whose
  refresh fails is dead, not silently kept.
- **`ServiceClient` cannot be aimed elsewhere:** it only sends below the configured base URL
  and never keeps a caller-supplied `Authorization` header.
- **Back-channel logout** accepts only a logout token that is signed by the issuer, issued for
  this client, carries the logout event and no `nonce`.
- **Lock file:** the SDK refuses to start when the manifest asks for more scopes than the
  review approved.
- **Icon:** served with a policy that forbids script in an SVG.
- **Static files** never leave the static directory (`..` and symlinks are refused).
- **Plain http is for this machine:** the public URL and the origins of the host app must use https
  unless they are loopback addresses, and so must the issuer outside `local`; the CLI warns before it
  sends a sign-in token to a store or an issuer over plain http.
- **Platform files hold no secret** and are not read by a deployment; the CLI's own credentials
  (`~/.config/appext/credentials.json`) are written with mode 0600 in a directory with mode 0700.

## What is on you

- Do not put secrets in the manifest, the platform file, the image or the repository. Keep `.appext/`
  and key files out of git (`.gitignore` and `.dockerignore` of the templates do).
- Your own routes need `ext.router()` / `ext.pages()`. A route added to the app by hand
  under `/api` is still covered by the CSRF rule, but not by the session check.
- Use `ext.require_role(...)` for decisions inside your extension; a target service does its own
  checks from the exchanged token (ideally the intersection of the person's rights and the scope's
  rights – never more than either).
- A `mode = "service"` service acts without a person: pass the person in the request if the
  target service needs to know, and keep such scopes narrow.
- `APPEXT_TRUSTED_PROXIES=*` believes `X-Forwarded-*` from everyone; name your ingress.
- Treat user-supplied data as data: the templates use `textContent` / autoescaping; keep it
  that way.

## What the platform has to protect

The store needs broad rights at the issuer (in Keycloak: `manage-clients`, `manage-realm`). A
compromised store could also change the host app's own client. Mitigations that the reference
implementation uses: the event log of every change at the issuer, four-eyes for restricted scopes,
running the store apart from extensions, and the store touching only clients with the prefix `ext-` and
scopes from the catalog (anything else is refused). Whether fine-grained admin permissions can narrow
the rights to `ext-*` clients is an open question there. A refresh token on a device that is unlocked
is a risk that the host app's lock and its logout mitigate. What a platform must do on its side is
listed in [platform-contract/README.md](platform-contract/README.md).

Vulnerabilities in the SDK: report them to the maintainers privately, not in a public tracker.
