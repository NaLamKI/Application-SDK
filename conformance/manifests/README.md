# Shared manifest cases

The store (`backend/app/store/manifest.py`) and the SDK (`appext.manifest`) judge an
`extension.toml` by the **same rules 1–6** of `concepts/app-store.md` §3. Both test
suites run these cases. Rule 7 (the service catalog) is the store's alone and has no
case here.

* `valid/*.toml` – must parse without error.
* `invalid/*.toml` – must fail, with **exactly** the error paths listed in `expected.json`.
* `expected.json` – `{"valid/minimal.toml": [], "invalid/r1-missing-id.toml": ["extension.id"], …}`.
  Compare the **sorted list of paths**; the messages are not part of the contract.

## Error paths

The path names the offending key in the TOML structure: tables joined by `.`, array
positions as `[n]` (zero based), a map key (a language) after a `.`.

| Case | Path |
|---|---|
| required key missing / wrong | `extension.id`, `extension.entry`, `services[1].mode` |
| the table itself missing | `extension` |
| an item of an array | `consent.scopes[0]`, `extension.hosts[2]`, `services[0].scopes[1]` |
| an entry of a `…_localized` table | `extension.name_localized.DE` |
| an unknown key (rule 6) | the full path of the key: `extension.colour`, `services[0].secret`, `surprise` |
| TOML that does not parse | the empty path `""` |
| duplicate scope (rule 2) | the path of the **later** occurrence; order is `consent.scopes`, then `services` in file order |
| duplicate service name (rule 3) | `services[n].name` of the later one |
| a service without a scope (rule 3) | `services[n].scopes` |

All violations are reported, not just the first. When a value has the wrong type *or*
the wrong shape, that is **one** error at its path (not one per rule).

## The rules, as patterns

| What | Rule |
|---|---|
| `extension.id` | `^[a-z][a-z0-9-]{1,38}[a-z0-9]$` |
| `extension.version`, `min_app_version` | `^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$` |
| `extension.entry` | starts with `/`, but **not** with `//` or `/\` (a browser reads both as another origin); a scheme (`https://…`) therefore fails too |
| `extension.name`, `icon` | non-empty string |
| scope names | `^[a-z][a-z0-9-]*$`, each at most once across `consent.scopes` and all `services[].scopes` |
| `services[].name` | `^[a-z][a-z0-9_]*$`, unique |
| `services[].mode` | **required**, `user` or `service` |
| `services[].audience` | required, non-empty |
| `services[].scopes` | required, at least one |
| `extension.audience_roles[]` | `^[A-Za-z][A-Za-z0-9_.-]{0,63}$` |
| `extension.hosts[]` | lower-case host name, no scheme, no path, no port: `^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$`, at most 253 characters |
| `extension.client_auth` | `private_key_jwt` (default) or `client_secret` |
| `extension.dev_port` | an integer (not a boolean) from 1024 to 65535 |
| `extension.display` | `in_app` (default) or `external`, exactly that spelling; where the app shows the extension |
| `…_localized` | a table `language → non-empty text`; language `^[a-z]{2}(-[A-Z]{2})?$` |
| unknown keys | an error, in every table (`extension`, `consent`, each `[[services]]`, and the top level) |
| `extension.kind` | `extension` (default) or `link`, exactly that spelling |
| a **link** (`kind = "link"`, rule 8): `extension.entry` | an absolute address: `https://…` (`http://` only for `127.0.0.1`, `localhost`, `::1`), plain ASCII, a host name, no user name or password, no whitespace, no backslash, at most 2048 characters |
| a link: `extension.icon` | **optional**; if present, an address by the same rules **on the same host as `entry`** (a file name is an error) |
| a link: `extension.display` | omitted or `external`; `in_app` is an error |
| a link: `extension.client_auth`, `dev_port`, `hosts` | **not allowed** – one error at the key's path, whatever the value |
| a link: `consent.scopes`, `services` | **not allowed** unless empty: errors at `consent.scopes` and `services` (one each, not one per item) |

Allowed top-level tables: `extension`, `consent`, `services`. Allowed `extension` keys:
`id name description version entry icon min_app_version hosts audience_roles client_auth
dev_port display kind name_localized description_localized`. `consent`: `scopes`. A service:
`name audience scopes mode`.

Rule 8 is the **link** rules (the cases `valid/link-*.toml` and `invalid/r8-*.toml`). A link is
an entry the app opens in the system browser; it has no server of the SDK behind it, so everything
that belongs to one (`icon` as a file, `client_auth`, `dev_port`, `hosts`, scopes, services) is
either replaced by an address or refused. Both implementations judge the same address rules
(`link_url_problem` in `appext.manifest` and in the store's `manifest.py`).

Whether `client_auth = "client_secret"` is *allowed* is a decision of the store's operator,
not a rule of the manifest: it is valid here.
