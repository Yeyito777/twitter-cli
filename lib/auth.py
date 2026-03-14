"""Extract Twitter auth tokens from qutebrowser-mnemo's cookie store."""

import sqlite3
from pathlib import Path

COOKIE_DB = Path.home() / ".runtime/qutebrowser-mnemo/data/webengine/Cookies"

# Static — same for all Twitter web users
BEARER_TOKEN = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)


def get_tokens():
    """Extract auth_token, ct0, and twid from the cookie store.

    Returns a dict with keys: auth_token, ct0, twid, bearer.
    Raises RuntimeError if critical tokens are missing.
    """
    if not COOKIE_DB.exists():
        raise RuntimeError(f"Cookie database not found: {COOKIE_DB}")

    conn = sqlite3.connect(f"file:{COOKIE_DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT name, value FROM cookies "
            "WHERE host_key LIKE '%x.com%' AND name IN ('auth_token', 'ct0', 'twid')"
        ).fetchall()
    finally:
        conn.close()

    tokens = {name: value for name, value in rows}
    tokens["bearer"] = BEARER_TOKEN

    missing = [k for k in ("auth_token", "ct0") if not tokens.get(k)]
    if missing:
        raise RuntimeError(
            f"Missing Twitter cookies: {', '.join(missing)}. "
            "Is Twitter logged in on qutebrowser-mnemo?"
        )

    return tokens


if __name__ == "__main__":
    try:
        t = get_tokens()
        print(f"auth_token: {t['auth_token'][:12]}...")
        print(f"ct0:        {t['ct0'][:20]}...")
        print(f"twid:       {t.get('twid', '(not found)')}")
        print(f"bearer:     {t['bearer'][:30]}...")
        print("\n✓ All tokens extracted successfully")
    except RuntimeError as e:
        print(f"✗ {e}")
