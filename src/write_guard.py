"""The one gate every Twitter mutation passes before it is sent.

``api.graphql_post`` and ``api.rest_post`` call :func:`ensure_writes_allowed`
before any network I/O, so no command, flag spelling, or helper can reach X
with a write unless:

- the invocation named its account explicitly with ``-a/--account``;
- the account has no lockout marker from an "automated"/locked response;
- the self-check passes (see :func:`self_check`): one coherent browser
  identity, Chrome TLS/HTTP2 impersonation, and a live web-client snapshot
  that yields an x-client-transaction-id and the operation's current query
  ID.  While any of that is broken every write stays paused, because requests
  that do not look like the web client are what gets accounts flagged and
  suspended.

Reads never pass through here.
"""

import json
import os
import time

# X answers that mean "stop writing now": continuing is how accounts get
# suspended.  Value: seconds the pause lasts, or None until the user clears it.
LOCKOUT_CODES = {
    226: None,          # "This request looks like it might be automated"
    326: None,          # account temporarily locked
    64: None,           # account suspended
    344: 24 * 60 * 60,  # daily limit for this action
    185: 24 * 60 * 60,  # over daily status update limit
    88: 15 * 60,        # rate limit exceeded
}


class WritePaused(RuntimeError):
    """A Twitter mutation was refused before anything was sent."""


def self_check(tokens, operation=None):
    """(problems, snapshot): why writes must stay paused (empty when healthy)."""
    from src import fingerprint, transport, webclient

    problems = list(fingerprint.identity_problems())
    impersonation = transport.impersonation_problem()
    if impersonation:
        problems.append(impersonation)
    snapshot = None
    if "alias" not in tokens:
        problems.append("credentials are not from a configured account")
    else:
        try:
            snapshot = webclient.load(tokens, tokens["alias"])
        except webclient.WebClientError as e:
            problems.append(f"the live X web client could not be loaded ({e})")
    if snapshot:
        try:
            webclient.transaction_id(snapshot, "POST", "/i/api/self-check")
        except Exception as e:
            problems.append(f"x-client-transaction-id cannot be generated ({e})")
        if operation and not operation.startswith("/") and not webclient.operation(snapshot, operation):
            problems.append(f"{operation} is not an operation of the live web client")
    return problems, snapshot


def _lockout_file(alias=None):
    from src.accounts import account_dir

    return account_dir(alias) / "write-lockout.json"


def active_lockout(alias=None):
    """The account's lockout marker while it is in force, else None."""
    try:
        marker = json.loads(_lockout_file(alias).read_text())
    except (OSError, ValueError):
        return None
    until = marker.get("until")
    if until is not None and time.time() >= until:
        return None
    return marker


def record_write_error(codes, message, operation):
    """Pause this account's writes after an answer from LOCKOUT_CODES."""
    hits = [code for code in codes if code in LOCKOUT_CODES]
    if not hits:
        return None
    durations = [LOCKOUT_CODES[code] for code in hits]
    until = None if None in durations else time.time() + max(durations)
    marker = {
        "codes": sorted(hits),
        "message": str(message)[:300],
        "operation": operation,
        "since": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "until": until,
    }
    from src.accounts import write_secure_json

    write_secure_json(_lockout_file(), marker)
    return marker


def clear_lockout(alias):
    try:
        _lockout_file(alias).unlink()
        return True
    except FileNotFoundError:
        return False


def ensure_writes_allowed(operation, tokens):
    """Raise WritePaused unless this invocation may send ``operation`` to X.

    Returns the live web-client snapshot the write must be signed with.
    """
    from src.accounts import ACCOUNT_EXPLICIT_ENV, load_registry

    if load_registry()["accounts"] and os.environ.get(ACCOUNT_EXPLICIT_ENV) != "1":
        raise WritePaused(
            f"Twitter writes require an explicit account: pass '-a/--account <alias>'. "
            f"Nothing was sent ({operation})."
        )
    marker = active_lockout()
    if marker:
        until = marker.get("until")
        lasts = (
            f"until {time.strftime('%Y-%m-%d %H:%M', time.localtime(until))}" if until
            else "until the user checks the account on x.com and runs 'twitter account unlock <alias>'"
        )
        raise WritePaused(
            f"Twitter writes for this account are paused {lasts}: X answered "
            f"{marker.get('operation')} with error {', '.join(map(str, marker.get('codes', [])))} "
            f"({marker.get('message', '')}). Nothing was sent ({operation})."
        )
    problems, snapshot = self_check(tokens, operation)
    if problems:
        raise WritePaused(
            f"Twitter writes are paused: the client self-check failed: {'; '.join(problems)}. "
            f"Nothing was sent ({operation}). Reading still works; see 'twitter doctor'."
        )
    return snapshot
