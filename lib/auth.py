"""Twitter auth token management.

Credentials are stored in $XDG_CONFIG_HOME/twitter-cli/credentials.json
(defaults to ~/.config/twitter-cli/credentials.json).

Run 'twitter setup' to configure.
"""

import json
import os
from pathlib import Path

# Static — same for all Twitter web users (this is the web app's client token)
BEARER_TOKEN = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)


def _config_dir():
    """Return the twitter-cli config directory, respecting XDG_CONFIG_HOME."""
    xdg = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(xdg) / "twitter-cli"


def _credentials_file():
    """Return the path to credentials.json."""
    return _config_dir() / "credentials.json"


def get_tokens():
    """Load auth_token and ct0 from credentials.json.

    Returns a dict with keys: auth_token, ct0, bearer.
    Raises RuntimeError if credentials are missing or the file doesn't exist.
    """
    path = _credentials_file()

    if not path.exists():
        raise RuntimeError(
            "Not authenticated. Run 'twitter setup' to configure your credentials."
        )

    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"Failed to read credentials file {path}: {e}")

    tokens = {
        "auth_token": data.get("auth_token", ""),
        "ct0": data.get("ct0", ""),
        "bearer": BEARER_TOKEN,
    }

    missing = [k for k in ("auth_token", "ct0") if not tokens[k]]
    if missing:
        raise RuntimeError(
            f"Credentials file is missing: {', '.join(missing)}. "
            "Run 'twitter setup' to reconfigure."
        )

    return tokens


def save_tokens(auth_token, ct0):
    """Save auth tokens to credentials.json.

    Creates the config directory if it doesn't exist.
    Sets restrictive file permissions (600) since these are credentials.
    """
    path = _credentials_file()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = {"auth_token": auth_token, "ct0": ct0}
    path.write_text(json.dumps(data, indent=2) + "\n")

    # Restrict permissions — these are session credentials
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows or restrictive filesystem


def setup_interactive():
    """Interactive setup flow — guides the user through extracting cookies."""
    print()
    print("  twitter-cli setup")
    print("  ─────────────────")
    print()
    print("  This tool needs two cookies from a logged-in x.com session:")
    print("  auth_token and ct0.")
    print()
    print("  To get them:")
    print()
    print("  1. Open https://x.com in your browser and log in")
    print("  2. Open DevTools (F12) → Application tab → Cookies → https://x.com")
    print("  3. Find and copy the values for 'auth_token' and 'ct0'")
    print()
    print("  (These are session cookies. They expire when you log out or after")
    print("  a period of inactivity. Just re-run 'twitter setup' if that happens.)")
    print()

    auth_token = input("  Paste auth_token: ").strip()
    if not auth_token:
        print("\n  ✗ auth_token is required.", flush=True)
        return False

    ct0 = input("  Paste ct0: ").strip()
    if not ct0:
        print("\n  ✗ ct0 is required.", flush=True)
        return False

    save_tokens(auth_token, ct0)
    path = _credentials_file()
    print()
    print(f"  ✓ Credentials saved to {path}")
    print("  You're all set! Try 'twitter tl' to see your timeline.")
    print()
    return True


if __name__ == "__main__":
    try:
        t = get_tokens()
        print(f"auth_token: {t['auth_token'][:12]}...")
        print(f"ct0:        {t['ct0'][:20]}...")
        print(f"bearer:     {t['bearer'][:30]}...")
        print("\n✓ All tokens loaded successfully")
    except RuntimeError as e:
        print(f"✗ {e}")
