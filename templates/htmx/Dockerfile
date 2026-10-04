# The base image comes from `sdk/docker/build.sh`; point this at your registry's copy
# with --build-arg APPEXT_IMAGE=registry.example.com/appext/python:0.1.0-py3.12
ARG APPEXT_IMAGE=appext/python:0.1.0-py3.12

# Server-rendered pages: no frontend build stage.
FROM ${APPEXT_IMAGE}
WORKDIR /app
COPY pyproject.toml extension.toml icon.svg ./
COPY app/ ./app/
COPY static/ ./static/
# The base image runs as the unprivileged user `appext`; installing is the one step that needs root.
USER root
RUN pip install --no-cache-dir .
USER appext
EXPOSE 8000
HEALTHCHECK CMD ["appext", "health"]
CMD ["appext", "serve", "app.main:app"]
