import os

# Tests must not depend on the deployment settings of the shell they run in:
# start from the local defaults, whatever APPEXT_* variables are exported.
for name in [n for n in os.environ if n.startswith("APPEXT_")]:
    del os.environ[name]
os.environ["APPEXT_ENV"] = "local"
