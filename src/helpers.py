"""Shared constants and utilities for the Twitter CLI."""

import re
import sys
from datetime import datetime, timezone

from src.api import graphql_get


# ─── GraphQL Query Hashes ─────────────────────────────────
# Extracted from client-web/main.*.js bundle on 2026-03-01.
# These change when Twitter deploys new code. See README.md
# for how to re-extract them.

Q = {
    "HomeTimeline":         "_J734qKR-wyeEf6vCZ1mfQ",
    "HomeLatestTimeline":   "csRxUH5ocwnJtPnB3-wr4g",
    "CreateTweet":          "y362cgN7cwMppu6Hy3JzrQ",
    "DeleteTweet":          "nxpZCY2K-I6QoFHAHeojFQ",
    "FavoriteTweet":        "lI07N6Otwv1PhnEgXILM7A",
    "UnfavoriteTweet":      "ZYKSe-w7KEslx3JhSIk5LA",
    "CreateRetweet":        "mbRO74GrOvSfRcJnlMapnQ",
    "DeleteRetweet":        "ZyZigVsNiFO6v1dEks1eWg",
    "CreateBookmark":       "aoDbu3RHznuiSkQ9aNM67Q",
    "DeleteBookmark":       "Wlmlj2-xzyS1GN3a6cj-mQ",
    "TweetDetail":          "ShZ7Ptnc5jM_23VVusteFw",
    "TweetResultByRestId":  "oSBAzPwnB3u5R9KqxACO3Q",
    "SearchTimeline":       "9AW3D-T7t9Vkvfdmq2L-iQ",
    "UserByScreenName":     "pLsOiyHJ1eFwPJlNmLp4Bg",
    "UserTweets":           "LhtwFV9WwCOurTanx8NNfg",
    "UserTweetsAndReplies": "9ESiiRo8Mhb_jqNIxduCgA",
    "Followers":            "W16HbbxU_8PjA_nE2JCr9g",
    "Following":            "ILoifaG-s7J3wWLd29oMSw",
    "Likes":                "W9r1yWJ5e9mGz6HMDHe8Vg",
    "NotificationsTimeline":"2yjiL84hBkOVpzJkkix6Mw",
    "Bookmarks":            "VFdMm9iVZxlU6hD86gfW_A",
    "ExplorePage":          "LERJigZ3fJPNFrouClO-RQ",
    "GenericTimelineById":  "JuyfK9IWpMEuu8K37e8N8Q",
}


# DM API parameters
DM_PARAMS = {
    "nsfw_filtering_enabled": "false",
    "filter_low_quality": "false",
    "include_quality": "all",
    "include_profile_interstitial_type": "1",
    "include_blocking": "1",
    "include_blocked_by": "1",
    "include_followed_by": "1",
    "include_want_retweets": "1",
    "include_mute_edge": "1",
    "include_can_dm": "1",
    "include_ext_is_blue_verified": "1",
    "include_ext_verified_type": "1",
    "include_ext_profile_image_shape": "1",
}


def parse_tweet_ref(ref):
    """Parse a tweet reference (URL or bare ID) into a tweet ID string.

    Accepts:
        https://x.com/user/status/1234567890
        https://twitter.com/user/status/1234567890
        1234567890

    Returns the bare ID string, or None if unparseable.
    """
    if not ref:
        return None
    m = re.search(r"/status/(\d+)", ref)
    if m:
        return m.group(1)
    if ref.isdigit():
        return ref
    return None


def require_tweet_ref(ref):
    """Parse a tweet ref or exit with error."""
    tweet_id = parse_tweet_ref(ref)
    if not tweet_id:
        print(f"Error: invalid tweet reference: {ref}", file=sys.stderr)
        print("Expected a tweet URL or numeric ID.", file=sys.stderr)
        sys.exit(1)
    return tweet_id


def compact_num(n):
    """Format numbers compactly: 1500 → 1.5K, 2300000 → 2.3M."""
    try:
        n = int(n)
    except (ValueError, TypeError):
        return str(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def format_dm_time(time_ms):
    """Convert DM timestamp (ms) to readable date."""
    try:
        dt = datetime.fromtimestamp(int(time_ms) / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OSError):
        return str(time_ms)


# Cache user ID lookups within a single invocation
_user_id_cache = {}


def resolve_user_id(screen_name):
    """Resolve a @username to a numeric user ID.

    Caches results for the lifetime of the process.
    Returns (user_id, user_result) tuple.
    """
    screen_name = screen_name.lstrip("@")
    if screen_name in _user_id_cache:
        return _user_id_cache[screen_name]

    data = graphql_get(
        Q["UserByScreenName"], "UserByScreenName",
        {"screen_name": screen_name, "withSafetyModeUserFields": True},
    )
    user_result = data.get("data", {}).get("user", {}).get("result", {})
    if not user_result or not user_result.get("rest_id"):
        print(f"Error: user @{screen_name} not found.", file=sys.stderr)
        sys.exit(1)

    user_id = user_result["rest_id"]
    _user_id_cache[screen_name] = (user_id, user_result)
    return user_id, user_result
