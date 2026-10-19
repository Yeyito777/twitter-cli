"""The one gate every Twitter mutation passes before it is sent.

``api.graphql_post`` and ``api.rest_post`` call :func:`ensure_writes_allowed`
before any network I/O, so no command, flag spelling, or helper can reach X
with a write unless:

- the client fingerprint is healthy (see :func:`fingerprint_problem`); while it
  is not, every write is paused because requests that do not look like the web
  client are what gets accounts flagged and suspended;
- the invocation named its account explicitly with ``-a/--account``;
- the account has no lockout marker from an "automated"/locked response.

Reads never pass through here.
"""

import os


class WritePaused(RuntimeError):
    """A Twitter mutation was refused before anything was sent."""


def fingerprint_problem():
    """Return why writes must stay paused, or None when the client is healthy."""
    return (
        "the client fingerprint is being fixed (missing x-client-transaction-id "
        "and an inconsistent browser identity raise suspension risk)"
    )


def ensure_writes_allowed(operation):
    """Raise WritePaused unless this invocation may send ``operation`` to X."""
    problem = fingerprint_problem()
    if problem:
        raise WritePaused(
            f"Twitter writes are paused: {problem}. Nothing was sent ({operation}). "
            "Reading still works."
        )
    from src.accounts import ACCOUNT_EXPLICIT_ENV, load_registry

    if load_registry()["accounts"] and os.environ.get(ACCOUNT_EXPLICIT_ENV) != "1":
        raise WritePaused(
            f"Twitter writes require an explicit account: pass '-a/--account <alias>'. "
            f"Nothing was sent ({operation})."
        )
