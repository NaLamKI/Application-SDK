from pathlib import Path

from appext.testing import configure_test_environment

# A test run must not depend on the machine it runs on (the platform file of the operator, the APPEXT_*
# variables of the shell). This runs before the tests import app.main, which reads its settings at import.
configure_test_environment(Path(__file__).resolve().parent.parent / "extension.toml")
