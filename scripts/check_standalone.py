"""Proves that the SDK works on its own, away from this repository and from any platform's backend.

It builds the wheel, installs it into a fresh virtual environment, and then plays an extension developer
on an imaginary platform ("Acme"): a platform file, a stand-in App Store on localhost, no other file of
this repository, an empty home directory and no `APPEXT_*` variable.

    python scripts/check_standalone.py

Needs network access for `pip` only. Exits non-zero at the first thing that does not work.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXTENSION = {"id": "hello", "name": "Hello", "status": "live", "liveVersion": "0.1.0", "clientId": "ext-hello",
             "clientAuth": "private_key_jwt", "hasKey": True, "versions": [], "urls": {}}


class StandInStore(BaseHTTPRequestHandler):
    """The smallest App Store that answers `appext store status` (it lives under `<api>/store`); it records what it was asked."""

    requests: list[tuple[str, str, str]] = []

    def do_GET(self) -> None:  # noqa: N802 (http.server's name)
        StandInStore.requests.append((self.command, self.path, self.headers.get("Authorization", "")))
        path = self.path.split("?")[0]
        if path == "/acme/api/v1/store/extensions":
            body = [EXTENSION]
        elif path.startswith("/acme/api/v1/store/extensions/"):
            body = {**EXTENSION, "id": path.rsplit("/", 1)[1]}
        else:
            self.send_error(404)
            return
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # keep the output readable
        pass


def step(text: str) -> None:
    print(f"\n== {text}", flush=True)


def fail(text: str) -> None:
    print(f"FAILED: {text}", file=sys.stderr)
    sys.exit(1)


def run(cmd: list[str], *, env: dict[str, str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        fail(f"{' '.join(cmd)}\n{result.stdout}\n{result.stderr}")
    return result


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="appext-standalone-") as tmp_name:
        tmp = Path(tmp_name)
        home, work, wheels = tmp / "home", tmp / "work", tmp / "wheels"
        for directory in (home, work, wheels):
            directory.mkdir()

        step("build the wheel and install it into a fresh environment")
        run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", str(wheels), str(REPO)],
            env=os.environ.copy(), cwd=tmp)
        wheel = next(wheels.glob("appext-*.whl"))
        run([sys.executable, "-m", "venv", str(tmp / "venv")], env=os.environ.copy(), cwd=tmp)
        bin_dir = tmp / "venv" / ("Scripts" if os.name == "nt" else "bin")
        run([str(bin_dir / "python"), "-m", "pip", "install", "-q", f"{wheel}[test]"], env=os.environ.copy(), cwd=tmp)

        # What an extension developer's shell has: no APPEXT_* variable, an empty home, nothing of this repository.
        env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HOME": str(home), "USERPROFILE": str(home),
               "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
        appext = str(bin_dir / "appext")

        step("with no platform configured, the commands say how to configure one")
        asked = run([appext, "store", "status"], env=env, cwd=work, check=False)
        if asked.returncode == 0 or "appext.toml" not in asked.stderr + asked.stdout:
            fail(f"expected an error that names appext.toml, got:\n{asked.stdout}\n{asked.stderr}")

        server = HTTPServer(("127.0.0.1", 0), StandInStore)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        store_url = f"http://127.0.0.1:{server.server_port}/acme/api/v1"
        (tmp / "acme.toml").write_text(f'''[platform]
name = "Acme Farm"
issuer = "https://auth.acme.example/realms/acme"
store_url = "{store_url}"

[platform.starter]
service = "acme"
audience = "acme-api"
scope = "ext-data-read"
''', encoding="utf-8")

        for template in ("spa", "htmx", "link"):
            step(f"appext new --template {template} --platform acme.toml")
            run([appext, "new", f"hello-{template}", "--template", template, "--platform", str(tmp / "acme.toml")],
                env=env, cwd=work)
            project = work / f"hello-{template}"
            if "acme" not in (project / "appext.toml").read_text(encoding="utf-8"):
                fail(f"{project / 'appext.toml'} does not carry the platform")
            if template != "link":
                step(f"the tests of the new {template} project run against nothing but the wheel")
                run([str(bin_dir / "python"), "-m", "pytest", "-q", "-p", "no:cacheprovider"], env=env, cwd=project)

        step("appext store status talks to the store the platform file names")
        project = work / "hello-spa"
        listing = run([appext, "store", "status"], env={**env, "APPEXT_STORE_TOKEN": "ci-token"}, cwd=project)
        if "hello-spa" not in listing.stdout or "live" not in listing.stdout:
            fail(f"the stand-in store's answer is missing:\n{listing.stdout}")
        if not any(path.startswith("/acme/api/v1/store/extensions") and auth == "Bearer ci-token"
                   for _, path, auth in StandInStore.requests):
            fail(f"the stand-in store was not asked, or without the token: {StandInStore.requests}")

        step("an environment variable beats the platform file")
        other = HTTPServer(("127.0.0.1", 0), StandInStore)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        StandInStore.requests.clear()
        override = f"http://127.0.0.1:{other.server_port}/acme/api/v1"
        run([appext, "store", "status"], env={**env, "APPEXT_STORE_TOKEN": "ci-token", "APPEXT_STORE_URL": override},
            cwd=project)
        if not StandInStore.requests:
            fail("APPEXT_STORE_URL was ignored")
        server.shutdown()
        other.shutdown()
    print("\nThe SDK works on its own.")


if __name__ == "__main__":
    main()
