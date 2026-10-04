# Base image

`appext/python:<version>-py3.12` is what every extension image builds on: Python 3.12
(slim), the `appext` SDK with uvicorn and the Redis client, and the unprivileged user
`appext` (uid 10001). Its default command is `appext serve app.main:app`, its health
check is `appext health`.

```sh
sdk/docker/build.sh          # builds the wheel, then appext/python:0.1.0-py3.12
```

An extension's Dockerfile starts `FROM` this image (see `sdk/templates/*/Dockerfile`).
Push the tag to your own registry and point the template at it with
`--build-arg APPEXT_IMAGE=registry.example.com/appext/python:0.1.0-py3.12`.

What the image does and does not do:

- **Unprivileged.** The image ends with `USER appext`. An extension switches to root for
  its own `pip install` and back.
- **Nothing secret inside.** Keys, the session key and the auth bundle are mounted or
  injected when the container starts (`docs/deployment.md`).
- **Proxy headers** (`X-Forwarded-For`, `-Proto`) are trusted only from the addresses in
  `APPEXT_TRUSTED_PROXIES` (comma-separated IPs or networks). Unset: from nobody.
- **Port** 8000, or `$PORT`.
- **One process per container.** Scale by running more containers; sessions live in Redis.
