# Changelog

## 0.1.0 – unreleased

The first version in this repository (the SDK was developed inside a product repository before).

- **Platform-neutral.** There is no built-in platform: the OAuth service and the App Store an extension
  is written for come from a platform file (`appext.toml`, named profiles under
  `~/.config/appext/platforms/`), environment variables and options ([docs/platform.md](docs/platform.md)).
- **Contract for platform builders** in [docs/platform-contract/](docs/platform-contract/README.md):
  what the OAuth service, the App Store and the host app have to provide.
- Extensions (`kind = "extension"`) and **links** (`kind = "link"`: an entry the host app opens in the
  system browser, no server of the SDK behind it); `display = "in_app" | "external"`.
- Sign-in (authorization code flow with PKCE, `private_key_jwt` or `client_secret`), server-side session,
  token exchange (RFC 8693) for calls to other services, back-channel logout.
- The bridge to a host app (`/_sdk/bridge.js`), with the host's name, labels and accent configurable.
- Command line: `appext new` (templates `spa`, `htmx`, `link`), `dev`, `serve`, `health`,
  `manifest check`, `keys`, `keycloak export`, `store …`.
- **Stands on its own.** `tests/library/test_standalone.py` and `scripts/check_standalone.py` (a CI job) check that
  nothing platform-specific or machine-specific is part of the repository and that the built wheel works in a
  fresh environment against a platform file and a stand-in store.
- Test helpers (`appext.testing`): a fake identity provider, a test client with a session, service mocks,
  `configure_test_environment`.
