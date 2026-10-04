# The base image comes from `sdk/docker/build.sh`; point this at your registry's copy
# with --build-arg APPEXT_IMAGE=registry.example.com/appext/python:0.1.0-py3.12
ARG APPEXT_IMAGE=appext/python:0.1.0-py3.12

# Stage 1: build the frontend
FROM node:22-alpine AS frontend
WORKDIR /src
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: runtime on the SDK base image
FROM ${APPEXT_IMAGE}
WORKDIR /app
COPY pyproject.toml extension.toml icon.svg ./
COPY app/ ./app/
# The base image runs as the unprivileged user `appext`; installing is the one step that needs root.
USER root
RUN pip install --no-cache-dir .
COPY --from=frontend /src/dist ./frontend/dist
USER appext
EXPOSE 8000
HEALTHCHECK CMD ["appext", "health"]
CMD ["appext", "serve", "app.main:app"]
