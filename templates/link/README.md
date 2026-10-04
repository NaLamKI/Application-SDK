# {{name}}

A **link** for {{platform_name}}, created with `appext new {{id}} --template link`: an entry in the
App Store that the host app opens in the system browser. There is no server of the SDK behind it:
no OAuth client, no key, no deployment, no sign-in, and the page receives nothing from the platform
– no token, no user, no account. The whole project is the manifest.

```
extension.toml         the manifest: id, name, version and the address to open
appext.toml            the platform: OAuth service, App Store (`appext store …` reads it)
```

Set `entry` in `extension.toml` to the address the app should open (an absolute `https`
address; `http` is only for `localhost`). The optional `icon` is an address on the **same host**
as `entry`, not a file. A link always opens in the browser, so there is no `display` to choose.
Every key is explained in `docs/manifest.md` of the SDK.

## Publish it

`appext.toml` has to name the platform's OAuth service (`issuer`) and App Store (`store_url`) first;
if it still has hints instead of values, fill them in.

```sh
appext manifest check                      # the manifest rules, including the link rules
appext store login
appext store register
appext store submit
# a reviewer approves it (the review is the only gate), then:
appext store verify                        # nothing to check: the link goes live at once
```

A link needs no `appext store key`, no auth bundle and no image. A new version whose `entry`
stays on the same host (and scheme and port) needs no new review; another host is
another link and goes back to review. The kind cannot change after registration: a link stays a
link, under its id.
