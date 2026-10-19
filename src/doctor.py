"""`twitter doctor`: read-only health report of the client fingerprint and write gate."""

import argparse
import time

from src import fingerprint, transport, webclient
from src.accounts import account_label, account_meta
from src.auth import get_tokens
from src.helpers import Q
from src.write_guard import active_lockout, self_check

WRITE_OPERATIONS = (
    "CreateTweet", "DeleteTweet", "FavoriteTweet", "UnfavoriteTweet", "CreateRetweet",
    "DeleteRetweet", "CreateBookmark", "DeleteBookmark",
)


def doctor(argv):
    p = argparse.ArgumentParser(
        allow_abbrev=False,
        prog="twitter doctor",
        description="Check the browser identity, the live web-client snapshot and whether writes may run. "
                    "Read-only: at most one x.com page load (plus cached client scripts).",
    )
    p.add_argument("--refresh", action="store_true", help="Reload the web-client snapshot now")
    args = p.parse_args(argv)

    meta = account_meta()
    tokens = get_tokens(meta["alias"])
    print(f"  Account: {account_label(meta)}")
    print(f"  Identity: {fingerprint.USER_AGENT}")
    print(f"  Client hints: {fingerprint.SEC_CH_UA}; platform {fingerprint.SEC_CH_UA_PLATFORM}; "
          f"TLS/HTTP2 as {fingerprint.IMPERSONATE}")
    if args.refresh and meta["alias"]:
        try:
            webclient.load(tokens, meta["alias"], refresh=True)
        except webclient.WebClientError:
            pass  # reported by the self-check below
    problems, snapshot = self_check(tokens)
    if snapshot:
        age = int(time.time() - snapshot["fetched_at"])
        print(f"  Web client: {snapshot['client']} (loaded {age // 60} min ago), "
              f"{len(snapshot['operations'])} operations")
        missing = [name for name in WRITE_OPERATIONS if not webclient.operation(snapshot, name)]
        if missing:
            problems.append(f"write operations missing from the live client: {', '.join(missing)}")
        drift = sorted(name for name, query_id in Q.items()
                       if webclient.operation(snapshot, name) and webclient.operation(snapshot, name)[0] != query_id)
        if drift:
            print(f"  Query IDs taken from the live client (built-in ones are stale): {', '.join(drift)}")
    marker = active_lockout(meta["alias"])
    if marker:
        problems.append(
            f"X answered {marker.get('operation')} with error {marker.get('codes')} at {marker.get('since')}"
            + ("" if marker.get("until") else "; clear with 'twitter account unlock <alias>' after checking x.com")
        )
    if transport.impersonation_problem() is None:
        print("  Transport: curl_cffi Chrome impersonation available")
    if problems:
        print("  Writes: PAUSED")
        for problem in problems:
            print(f"    - {problem}")
    else:
        print("  Writes: allowed (with an explicit -a/--account)")
