"""Twitter auth token management.

Each account's credentials are stored in config/accounts/<alias>/credentials.json
(an unmigrated single login uses config/credentials.json).
Run 'twitter account add' or 'twitter -a <alias> login' to configure.
"""

import argparse
import getpass
import json
import subprocess
import sys
import urllib.parse

from src.accounts import (
    AccountError,
    account_dir,
    account_meta,
    selected_alias,
    write_secure_json,
)

# Static — same for all Twitter web users (this is the web app's client token)
BEARER_TOKEN = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)


def credentials_file(alias=None):
    return account_dir(alias) / "credentials.json"


def get_tokens(alias=None):
    """Load auth_token and ct0 for an account (default: the selected one).

    Returns a dict with keys: auth_token, ct0, bearer.
    Raises RuntimeError if credentials are missing or the file doesn't exist.
    """
    alias = alias or selected_alias()
    path = credentials_file(alias)
    relogin = f"twitter -a {alias} login" if alias else "twitter login"
    if not path.exists():
        raise RuntimeError(f"Not authenticated. Run '{relogin}' to configure your credentials.")

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
            f"Run '{relogin}' to reconfigure."
        )

    return tokens


def save_tokens(tokens, alias=None):
    """Save an account's auth cookies with owner-only permissions."""
    write_secure_json(credentials_file(alias), {"auth_token": tokens["auth_token"], "ct0": tokens["ct0"]})


# ─── Credential sources ───────────────────────────────────


def add_credential_args(parser):
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--from-vimbrowser",
        action="store_true",
        help="Import the account's x.com login from vimbrowser's cookies",
    )
    source.add_argument(
        "--stdin",
        action="store_true",
        help='Read {"auth_token": ..., "ct0": ...} JSON from stdin',
    )


def read_credentials(args, *, handle=None, user_id=None):
    if args.from_vimbrowser:
        return vimbrowser_credentials(handle=handle, user_id=user_id)
    if args.stdin:
        try:
            data = json.loads(sys.stdin.read())
        except json.JSONDecodeError as e:
            raise AccountError(f"stdin is not valid JSON: {e}") from e
        tokens = {k: str(data.get(k) or "").strip() for k in ("auth_token", "ct0")}
    else:
        tokens = _prompt_credentials()
    missing = [k for k, v in tokens.items() if not v]
    if missing:
        raise AccountError(f"Missing {', '.join(missing)}.")
    return {**tokens, "bearer": BEARER_TOKEN}


def _prompt_credentials():
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
    print("  a period of inactivity. Just log in again if that happens.)")
    print()
    return {
        "auth_token": getpass.getpass("  Paste auth_token: ").strip(),
        "ct0": getpass.getpass("  Paste ct0: ").strip(),
    }


def vimbrowser_credentials(*, handle=None, user_id=None):
    """Read one x.com login from vimbrowser's cookie jar.

    X keeps the active login in auth_token/ct0 (identified by twid) and every
    other signed-in account as a "user_id:auth_token" entry in auth_multi.
    """
    try:
        output = subprocess.run(
            ["vimbrowser-cli", "cookies", "--url", "https://x.com"],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout
        cookies = {c["name"]: c["value"] for c in json.loads(output)["cookies"]}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as e:
        raise AccountError(f"Could not read x.com cookies from vimbrowser: {e}") from e

    sessions = {}
    active_id = urllib.parse.unquote(cookies.get("twid", "")).removeprefix("u=")
    if active_id.isdigit() and cookies.get("auth_token") and cookies.get("ct0"):
        sessions[active_id] = {"auth_token": cookies["auth_token"], "ct0": cookies["ct0"]}
    for entry in urllib.parse.unquote(cookies.get("auth_multi", "")).strip('"').split("|"):
        entry_id, _, token = entry.partition(":")
        if entry_id.isdigit() and token and entry_id not in sessions:
            sessions[entry_id] = {"auth_token": token, "ct0": ""}
    if not sessions:
        raise AccountError("vimbrowser is not logged in to x.com.")

    def credentials(session):
        if not session["ct0"]:
            # The browser's ct0 belongs to its active login; other logins need their own.
            session["ct0"] = _issue_ct0(session["auth_token"])
        return {**session, "bearer": BEARER_TOKEN}

    if user_id is not None:
        if str(user_id) not in sessions:
            raise AccountError(f"vimbrowser is not logged in to X user {user_id}; add it on x.com first.")
        return credentials(sessions[str(user_id)])
    # Ask each login to look the handle up itself: X hides suspended accounts
    # from everyone except their owner.
    for session_id, session in sessions.items():
        tokens = credentials(session)
        try:
            if lookup_user(handle, tokens)["user_id"] == session_id:
                return tokens
        except AccountError:
            continue
    raise AccountError(f"vimbrowser is not logged in to @{str(handle).lstrip('@')}; add it on x.com first.")


def _issue_ct0(auth_token):
    """Ask x.com for a CSRF token bound to this auth_token."""
    import requests
    from x_client_transaction.utils import generate_headers

    session = requests.Session()
    session.headers.update(generate_headers())
    session.cookies.set("auth_token", auth_token, domain=".x.com", path="/")
    session.get("https://x.com/home", timeout=30)
    issued = [cookie.value for cookie in session.cookies if cookie.name == "ct0"]
    if not issued:
        raise AccountError("x.com did not issue a ct0 for that login; it may have expired.")
    return issued[-1]


# ─── Identity ─────────────────────────────────────────────


def viewer_id(tokens):
    """Return the X user ID these credentials are logged in as."""
    from src.api import graphql_get
    from src.helpers import Q

    data = graphql_get(
        Q["NotificationsTimeline"], "NotificationsTimeline",
        {"timeline_type": "All", "count": 1}, tokens=tokens,
    )
    user_id = str(
        data.get("data", {}).get("viewer_v2", {}).get("user_results", {})
        .get("result", {}).get("rest_id") or ""
    )
    if not user_id.isdigit():
        raise AccountError("x.com did not accept that login.")
    return user_id


def lookup_user(handle, tokens):
    from src.api import graphql_get
    from src.helpers import Q

    screen_name = str(handle or "").lstrip("@")
    if not screen_name:
        raise AccountError("A @handle is required.")
    data = graphql_get(
        Q["UserByScreenName"], "UserByScreenName",
        {"screen_name": screen_name, "withSafetyModeUserFields": True}, tokens=tokens,
    )
    user = data.get("data", {}).get("user", {}).get("result", {})
    if user.get("__typename") == "UserUnavailable":
        raise AccountError(f"User @{screen_name} is unavailable: {user.get('reason') or user.get('message')}.")
    if not user.get("rest_id"):
        raise AccountError(f"User @{screen_name} not found.")
    core, legacy = user.get("core", {}), user.get("legacy", {})
    return {
        "user_id": str(user["rest_id"]),
        "screen_name": core.get("screen_name") or legacy.get("screen_name") or screen_name,
        "name": core.get("name") or legacy.get("name") or "",
    }


def verify_identity(tokens, *, handle):
    """Confirm the credentials are logged in as @handle and return its identity."""
    logged_in_as = viewer_id(tokens)
    identity = lookup_user(handle, tokens)
    if identity["user_id"] != logged_in_as:
        raise AccountError(
            f"That login is X user {logged_in_as}, not @{identity['screen_name']} ({identity['user_id']})."
        )
    return identity


# ─── Commands ─────────────────────────────────────────────


def login(argv):
    """Store fresh credentials for the selected account."""
    p = argparse.ArgumentParser(
        prog="twitter login",
        description="Refresh the selected account's x.com login (pick one with -a/--account).",
    )
    add_credential_args(p)
    args = p.parse_args(argv)

    try:
        meta = account_meta()
        if meta["alias"] is None:
            if args.from_vimbrowser:
                raise AccountError("--from-vimbrowser needs a named account; see 'twitter account --help'.")
            tokens = read_credentials(args)
        else:
            tokens = read_credentials(args, user_id=meta["user_id"])
            logged_in_as = viewer_id(tokens)
            if logged_in_as != meta["user_id"]:
                raise AccountError(
                    f"That login is X user {logged_in_as}, not '{meta['alias']}' ({meta['user_id']}). "
                    "Add it as a separate account instead."
                )
    except AccountError as e:
        print(f"\n  ✗ {e}", file=sys.stderr)
        sys.exit(1)

    save_tokens(tokens, meta["alias"])
    print()
    print(f"  ✓ Credentials saved to {credentials_file(meta['alias'])}")
    print("  You're all set! Try 'twitter timeline' to see your feed.")
    print()


def logout(argv):
    """Remove the selected account's stored credentials."""
    p = argparse.ArgumentParser(prog="twitter logout")
    p.parse_args(argv)
    path = credentials_file()
    if path.exists():
        path.unlink()
        print("Logged out.")
    else:
        print("Not logged in.")
