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


def _no_network(method, url, **_kwargs):
    raise AssertionError(f"test tried to reach the network: {method} {url}")


# Tests patch src.transport.request with fakes; anything unpatched fails loudly
# instead of talking to x.com with test credentials.
from src import transport as _transport  # noqa: E402

_transport.request = _no_network
