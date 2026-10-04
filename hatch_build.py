"""Build hook: ship `templates/` and `docker/` inside the wheel.

They are real directories in the repository (owned by other work), and a
directory that does not exist yet must not break the build – hence a hook that
looks first, instead of a static `force-include` that fails on a missing path.
Editable installs skip it: there the CLI reads the directories from the source
tree.
"""
from __future__ import annotations

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

#: source directory (relative to this file) -> place inside the package
SHIPPED = {"templates": "appext/_templates", "docker": "appext/_docker"}

#: Never ship these, whatever is lying around in a developer's working copy:
#: keys, caches, build output.
SKIP_PARTS = {"node_modules", "__pycache__", ".appext", ".venv", "dist", "keys", ".git"}
SKIP_SUFFIXES = {".pyc", ".pem", ".key"}


class ShipDirectories(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        if version == "editable":
            return
        root = Path(self.root)
        for source, target in SHIPPED.items():
            base = root / source
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*")):
                relative = path.relative_to(base)
                if not path.is_file() or SKIP_PARTS & set(relative.parts) or path.suffix in SKIP_SUFFIXES:
                    continue
                build_data["force_include"][str(path)] = f"{target}/{relative.as_posix()}"
