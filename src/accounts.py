"""Named Twitter/X account profiles and per-account state.

``TWITTER_ACCOUNT`` selects an account for one invocation; ``bin/twitter`` sets
it from ``-a/--account``.  Reads fall back to the default account, but once
several accounts exist every mutation must name its account explicitly, so
nothing is ever posted as the wrong user by accident.

An installation without ``accounts.json`` keeps its single legacy login in
``config/credentials.json`` until ``twitter account migrate`` names it.
"""

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_ROOT = Path(os.environ.get("TWITTER_CONFIG_DIR", "").strip() or (PROJECT_ROOT / "config"))
ACCOUNT_ENV = "TWITTER_ACCOUNT"
ACCOUNT_EXPLICIT_ENV = "TWITTER_ACCOUNT_EXPLICIT"
OWNERS = ("user", "assistant", "other")
# Files that belong to one login rather than to the whole installation.
ACCOUNT_FILES = ("credentials.json", "agent-tweets.json", "notify-state.json")
_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

MUTATING_COMMANDS = {
    "post", "reply", "delete",
    "like", "unlike", "rt", "unrt", "bookmark", "unbookmark",
    "follow", "unfollow", "mute", "unmute", "block", "unblock",
    "login", "logout",
}
MUTATING_NOTIFY_COMMANDS = {"subscribe", "unsubscribe", "add", "remove", "mark", "unmark"}


class AccountError(RuntimeError):
    pass


# ─── Registry ─────────────────────────────────────────────


def _registry_file():
    return CONFIG_ROOT / "accounts.json"


def legacy_credentials_file():
    return CONFIG_ROOT / "credentials.json"


def normalize_alias(alias):
    value = str(alias or "").strip().lower()
    if not _ALIAS_RE.fullmatch(value):
        raise AccountError(
            "Account aliases must start with a letter or number and contain only "
            "lowercase letters, numbers, '.', '_', or '-'."
        )
    return value


def load_registry():
    path = _registry_file()
    if not path.exists():
        return {"version": 1, "default_account": None, "accounts": {}}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise AccountError(f"Failed to read account registry {path}: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
        raise AccountError(f"Invalid account registry: {path}")
    for alias, meta in data["accounts"].items():
        if normalize_alias(alias) != alias:
            raise AccountError(f"Invalid account alias in registry: {alias!r}")
        if not isinstance(meta, dict) or not str(meta.get("user_id") or "").isdigit():
            raise AccountError(f"Invalid metadata for Twitter account '{alias}'.")
    data.setdefault("version", 1)
    if data.get("default_account") not in data["accounts"]:
        data["default_account"] = None
    return data


def write_secure_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def save_registry(registry):
    write_secure_json(_registry_file(), registry)


# ─── Selection and paths ──────────────────────────────────


def selected_alias():
    """Return the account this invocation acts as, or None for a legacy single login."""
    registry = load_registry()
    accounts = registry["accounts"]
    requested = os.environ.get(ACCOUNT_ENV, "").strip()
    if requested:
        alias = normalize_alias(requested)
        if alias not in accounts:
            choices = ", ".join(sorted(accounts)) or "(none)"
            raise AccountError(f"Twitter account '{alias}' is not configured. Available: {choices}")
        return alias
    if registry["default_account"]:
        return registry["default_account"]
    if len(accounts) == 1:
        return next(iter(accounts))
    if accounts:
        raise AccountError(
            "No default Twitter account is set. Pass '-a/--account <alias>' "
            "or run 'twitter account default <alias>'."
        )
    return None


def account_dir(alias=None):
    """Directory holding one account's files; None means the selected account."""
    alias = normalize_alias(alias) if alias else selected_alias()
    return CONFIG_ROOT if alias is None else CONFIG_ROOT / "accounts" / alias


def account_meta(alias=None):
    """Registry metadata plus 'alias'; an empty legacy profile for an unmigrated login."""
    alias = normalize_alias(alias) if alias else selected_alias()
    if alias is None:
        return {"alias": None}
    registry = load_registry()
    if alias not in registry["accounts"]:
        raise AccountError(f"Twitter account '{alias}' is not configured.")
    return {**registry["accounts"][alias], "alias": alias}


def notify_aliases():
    """Accounts the notification poller should watch."""
    accounts = load_registry()["accounts"]
    if accounts:
        return [alias for alias in sorted(accounts) if (account_dir(alias) / "credentials.json").exists()]
    return [None] if legacy_credentials_file().exists() else []


def account_label(meta):
    alias = meta.get("alias")
    if alias is None:
        return "legacy login"
    handle = meta.get("screen_name") or meta.get("user_id") or "unknown"
    name = str(meta.get("name") or "").strip()
    identity = f"{name} (@{handle})" if name and name != handle else f"@{handle}"
    return f"{alias} — {identity} [{meta.get('owner', 'unknown')}]"


# ─── Per-invocation policy ────────────────────────────────


def _is_mutating(cmd, argv):
    if cmd == "dm":
        # argparse also accepts '--s'/'--se'/'--sen' and bundled short flags
        # such as '-hs', so treat any of those as a send.
        return any(
            value.startswith("--s") or (value.startswith("-") and not value.startswith("--") and "s" in value)
            for value in argv
        )
    if cmd == "notify":
        return bool(argv) and argv[0] in MUTATING_NOTIFY_COMMANDS
    return cmd in MUTATING_COMMANDS


def preflight(cmd, argv):
    """Validate account selection and print which account the command acts as."""
    if any(value in ("-h", "--help") for value in argv):
        return
    accounts = load_registry()["accounts"]
    explicit = os.environ.get(ACCOUNT_EXPLICIT_ENV) == "1"
    if not accounts and not explicit:
        return
    if not explicit and _is_mutating(cmd, argv):
        # Even with one account: that account may be the user's own.
        choices = "\n".join(
            f"  {account_label({**meta, 'alias': alias})}" for alias, meta in sorted(accounts.items())
        )
        raise AccountError(
            "This command writes to Twitter and requires '-a/--account <alias>', "
            "even with one account.\n" + choices
        )
    meta = account_meta()
    if len(accounts) > 1 or explicit:
        print(f"Twitter account: {account_label(meta)}", file=sys.stderr)


# ─── Account commands ─────────────────────────────────────


def _identity_meta(identity, owner, notify_source=None):
    meta = {
        "user_id": identity["user_id"],
        "screen_name": identity["screen_name"],
        "name": identity["name"],
        "owner": owner,
    }
    if notify_source:
        meta["notify_source"] = notify_source
    return meta


def add_account(argv):
    from src.auth import add_credential_args, read_credentials, save_tokens, verify_identity

    p = argparse.ArgumentParser(
        allow_abbrev=False,
        prog="twitter account add",
        description="Add a named Twitter/X login.",
        epilog="Example: twitter account add paramount --handle im_paramount --owner assistant --from-vimbrowser",
    )
    p.add_argument("alias", help="Short local name used with -a/--account")
    p.add_argument("--handle", required=True, help="The account's @handle; the login must belong to it")
    p.add_argument("--owner", choices=OWNERS, required=True, help="Who the account belongs to")
    p.add_argument("--default", action="store_true", help="Make this the default account for reads")
    add_credential_args(p)
    args = p.parse_args(argv)

    alias = normalize_alias(args.alias)
    registry = load_registry()
    if alias in registry["accounts"]:
        raise AccountError(f"Twitter account alias '{alias}' already exists.")
    if not registry["accounts"] and legacy_credentials_file().exists():
        raise AccountError(
            "Name the existing login first with 'twitter account migrate <alias> --handle ... --owner ...'."
        )
    for other, meta in registry["accounts"].items():
        if meta.get("screen_name", "").lower() == args.handle.lstrip("@").lower():
            raise AccountError(f"@{args.handle.lstrip('@')} is already configured as '{other}'.")

    tokens = read_credentials(args, handle=args.handle)
    identity = verify_identity(tokens, handle=args.handle)
    save_tokens(tokens, alias)
    registry["accounts"][alias] = _identity_meta(identity, args.owner)
    if args.default or not registry["default_account"]:
        registry["default_account"] = alias
    save_registry(registry)
    print(f"Added Twitter account: {account_label({**registry['accounts'][alias], 'alias': alias})}")


def migrate_legacy(argv):
    from src.auth import get_tokens, verify_identity
    from src.notify import NOTIFICATION_SOURCE_ID, find_notify_pids

    p = argparse.ArgumentParser(
        allow_abbrev=False,
        prog="twitter account migrate",
        description="Name the legacy single login, keeping its managed tweets and notification subscriptions.",
    )
    p.add_argument("alias")
    p.add_argument("--handle", required=True, help="The legacy login's @handle")
    p.add_argument("--owner", choices=OWNERS, required=True)
    args = p.parse_args(argv)

    alias = normalize_alias(args.alias)
    registry = load_registry()
    if registry["accounts"]:
        raise AccountError("Accounts are already configured; there is no legacy login to migrate.")
    if not legacy_credentials_file().exists():
        raise AccountError(f"No legacy credentials found at {legacy_credentials_file()}")
    pids = find_notify_pids()
    if pids:
        raise AccountError(
            f"The notification poller is running (PID {', '.join(map(str, pids))}). "
            "Run 'twitter notify stop' first and 'twitter notify start' afterwards."
        )

    identity = verify_identity(get_tokens(), handle=args.handle)
    target = CONFIG_ROOT / "accounts" / alias
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ACCOUNT_FILES:
        if (CONFIG_ROOT / name).exists():
            shutil.move(str(CONFIG_ROOT / name), str(target / name))
    # Existing Exocortex subscriptions stay attached to the original source.
    registry["accounts"][alias] = _identity_meta(identity, args.owner, notify_source=NOTIFICATION_SOURCE_ID)
    registry["default_account"] = alias
    save_registry(registry)
    print(f"Migrated Twitter account: {account_label({**registry['accounts'][alias], 'alias': alias})}")


def list_accounts(argv):
    p = argparse.ArgumentParser(allow_abbrev=False, prog="twitter accounts", description="List configured Twitter accounts.")
    p.parse_args(argv)
    registry = load_registry()
    if not registry["accounts"]:
        if legacy_credentials_file().exists():
            print("  legacy login (name it with 'twitter account migrate <alias> --handle ... --owner ...')")
        else:
            print("  No Twitter accounts configured.")
        return
    for alias, meta in sorted(registry["accounts"].items()):
        notes = []
        if alias == registry["default_account"]:
            notes.append("default")
        if not (account_dir(alias) / "credentials.json").exists():
            notes.append(f"logged out; run 'twitter -a {alias} login'")
        suffix = f"; {', '.join(notes)}" if notes else ""
        print(f"  {account_label({**meta, 'alias': alias})}{suffix}")


def set_default(argv):
    p = argparse.ArgumentParser(allow_abbrev=False, prog="twitter account default", description="Set the default account for reads.")
    p.add_argument("alias")
    args = p.parse_args(argv)
    alias = normalize_alias(args.alias)
    registry = load_registry()
    if alias not in registry["accounts"]:
        raise AccountError(f"Twitter account '{alias}' is not configured.")
    registry["default_account"] = alias
    save_registry(registry)
    print(f"Default Twitter account: {alias}")


def remove_account(argv):
    from src.exocortex import list_external_notification_subscriptions, unsubscribe_external_notification
    from src.notify import TOOL_NAME, notification_source

    p = argparse.ArgumentParser(
        allow_abbrev=False,
        prog="twitter account remove",
        description="Remove an account's credentials, managed tweets, and notification subscriptions.",
    )
    p.add_argument("alias")
    p.add_argument("--yes", action="store_true", help="Confirm removal")
    args = p.parse_args(argv)
    alias = normalize_alias(args.alias)
    if not args.yes:
        raise AccountError("Account removal requires --yes.")
    registry = load_registry()
    if alias not in registry["accounts"]:
        raise AccountError(f"Twitter account '{alias}' is not configured.")

    source_id = notification_source(alias)["id"]
    try:
        for subscription in list_external_notification_subscriptions(tool_name=TOOL_NAME, source_id=source_id):
            unsubscribe_external_notification(subscription_id=subscription["id"])
    except RuntimeError as e:
        print(f"Warning: could not remove notification subscriptions for {source_id}: {e}", file=sys.stderr)

    shutil.rmtree(account_dir(alias), ignore_errors=True)
    del registry["accounts"][alias]
    if registry["default_account"] == alias:
        registry["default_account"] = next(iter(sorted(registry["accounts"])), None)
    save_registry(registry)
    print(f"Removed Twitter account: {alias}")


def unlock_account(argv):
    from src.write_guard import active_lockout, clear_lockout

    p = argparse.ArgumentParser(
        allow_abbrev=False,
        prog="twitter account unlock",
        description="Clear the write pause X's automated/locked answer put on an account. "
                    "Only after checking the account on x.com.",
    )
    p.add_argument("alias")
    args = p.parse_args(argv)
    alias = normalize_alias(args.alias)
    if alias not in load_registry()["accounts"]:
        raise AccountError(f"Twitter account '{alias}' is not configured.")
    marker = active_lockout(alias)
    print(f"Cleared write pause for {alias}." if clear_lockout(alias) else f"{alias} had no write pause.")
    if marker:
        print(f"  It was set by error {marker.get('codes')} on {marker.get('operation')} at {marker.get('since')}.")


_COMMANDS = {
    "list": list_accounts,
    "ls": list_accounts,
    "add": add_account,
    "migrate": migrate_legacy,
    "default": set_default,
    "remove": remove_account,
    "unlock": unlock_account,
}


def dispatch(argv):
    if not argv:
        list_accounts([])
        return
    subcmd, rest = argv[0], argv[1:]
    if subcmd in ("-h", "--help", "help"):
        print("usage: twitter account <list|add|migrate|default|remove|unlock> [args]")
        print("Run 'twitter account <subcommand> --help' for details.")
        return
    fn = _COMMANDS.get(subcmd)
    if fn is None:
        raise AccountError(f"Unknown account subcommand '{subcmd}'.")
    fn(rest)


def cli_preflight(cmd, argv):
    try:
        preflight(cmd, argv)
    except AccountError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)


def cli_dispatch(argv):
    try:
        dispatch(argv)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1)
