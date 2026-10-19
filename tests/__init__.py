"""Twitter CLI tests.

Point the tool at an empty config before any test imports ``src``: no test
may read the real accounts or credentials.
"""

import os
import tempfile

_CONFIG = tempfile.TemporaryDirectory(prefix="twitter-cli-tests-")
os.environ["TWITTER_CONFIG_DIR"] = _CONFIG.name
for _name in ("TWITTER_ACCOUNT", "TWITTER_ACCOUNT_EXPLICIT"):
    os.environ.pop(_name, None)
