"""Twitter auth token management.

Credentials are stored in config/credentials.json (project-local).
Run 'twitter login' to configure.
"""

import json
import os
import sys
from pathlib import Path

# Config dir: PROJECT_ROOT/config
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"

# Static — same for all Twitter web users (this is the web app's client token)
BEARER_TOKEN = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)


def get_tokens():
    """Load auth_token and ct0 from credentials.json.

    Returns a dict with keys: auth_token, ct0, bearer.
    Raises RuntimeError if credentials are missing or the file doesn't exist.
    """
    if not CREDENTIALS_FILE.exists():
        raise RuntimeError(
            "Not authenticated. Run 'twitter login' to configure your credentials."
        )

    try:
        data = json.loads(CREDENTIALS_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"Failed to read credentials file {CREDENTIALS_FILE}: {e}")

    tokens = {
        "auth_token": data.get("auth_token", ""),
        "ct0": data.get("ct0", ""),
        "bearer": BEARER_TOKEN,
    }

    missing = [k for k in ("auth_token", "ct0") if not tokens[k]]
    if missing:
        raise RuntimeError(
            f"Credentials file is missing: {', '.join(missing)}. "
            "Run 'twitter login' to reconfigure."
        )

    return tokens


def _save_tokens(auth_token, ct0):
    """Save auth tokens to credentials.json.

    Creates the config directory if it doesn't exist.
    Sets restrictive file permissions (600) since these are credentials.
    """
    CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)

    data = {"auth_token": auth_token, "ct0": ct0}
    CREDENTIALS_FILE.write_text(json.dumps(data, indent=2) + "\n")

    try:
        os.chmod(CREDENTIALS_FILE, 0o600)
    except OSError:
        pass


def login(argv):
    """Interactive setup — configure auth credentials."""
    import argparse
    p = argparse.ArgumentParser(prog="twitter login",
        description="Authenticate by pasting browser cookies from x.com.")
    p.parse_args(argv)

    print()
    print("  twitter login")
    print("  ─────────────")
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
    print("  a period of inactivity. Just re-run 'twitter login' if that happens.)")
    print()

    auth_token = input("  Paste auth_token: ").strip()
    if not auth_token:
        print("\n  ✗ auth_token is required.", flush=True)
        sys.exit(1)

    ct0 = input("  Paste ct0: ").strip()
    if not ct0:
        print("\n  ✗ ct0 is required.", flush=True)
        sys.exit(1)

    _save_tokens(auth_token, ct0)
    print()
    print(f"  ✓ Credentials saved to {CREDENTIALS_FILE}")
    print("  You're all set! Try 'twitter timeline' to see your feed.")
    print()


def logout(argv):
    """Remove stored credentials."""
    if CREDENTIALS_FILE.exists():
        CREDENTIALS_FILE.unlink()
        print("Logged out.")
    else:
        print("Not logged in.")
