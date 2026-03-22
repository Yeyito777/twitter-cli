"""Format parsed Twitter data for terminal output."""


def _compact_number(n):
    """Format large numbers compactly: 1234 -> 1.2K, 1234567 -> 1.2M."""
    try:
        n = int(n)
    except (ValueError, TypeError):
        return str(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def format_tweet(tweet, indent=0):
    """Format a single tweet for terminal display."""
    pad = " " * indent
    lines = []

    handle = tweet["handle"]
    name = tweet["name"]
    date = tweet["created_at"]
    tweet_id = tweet["id"]

    # Header
    lines.append(f"{pad}@{handle} ({name})  {date}")
    lines.append(f"{pad}https://x.com/{handle}/status/{tweet_id}")

    # If retweet, show original
    if tweet["is_retweet"] and tweet.get("retweeted"):
        rt = tweet["retweeted"]
        lines.append(f"{pad}  🔁 Retweeted @{rt['handle']}:")
        lines.append("")
        lines.append(format_tweet(rt, indent=indent + 4))
        return "\n".join(lines)

    # Tweet text
    text = tweet["text"]
    if text:
        for line in text.split("\n"):
            lines.append(f"{pad}  {line}")

    # Quoted tweet
    if tweet["is_quote"] and tweet.get("quoted"):
        qt = tweet["quoted"]
        lines.append(f"{pad}  ┌─ Quoting @{qt['handle']}:")
        for line in qt["text"].split("\n"):
            lines.append(f"{pad}  │ {line}")
        lines.append(f"{pad}  └─ ♥ {_compact_number(qt['likes'])}  🔁 {_compact_number(qt['retweets'])}")

    # Media
    if tweet["media"]:
        for m in tweet["media"]:
            lines.append(f"{pad}  📎 [{m['type']}] {m['url']}")

    # Reply context
    if tweet["is_reply"] and tweet["in_reply_to"]:
        lines.append(f"{pad}  ↩ replying to @{tweet['in_reply_to']}")

    # Engagement
    stats = (
        f"♥ {_compact_number(tweet['likes'])}  "
        f"🔁 {_compact_number(tweet['retweets'])}  "
        f"💬 {_compact_number(tweet['replies'])}  "
        f"👁 {_compact_number(tweet['views'])}"
    )
    lines.append(f"{pad}  {stats}")

    return "\n".join(lines)


def format_notification(notif):
    """Format a single notification for terminal display."""
    icon = notif["icon"]
    message = notif["message"]
    date = notif["created_at"]
    url = notif.get("url", "")

    lines = []
    lines.append(f"{icon}  {message}  {date}")
    if url:
        lines.append(f"  {url}")
    return "\n".join(lines)


def format_trend(trend):
    """Format a single trend for terminal display."""
    rank = trend.get("rank", "")
    name = trend["name"]
    domain = trend.get("domain", "")
    desc = trend.get("description", "")

    line = f"  {rank:>2}. {name}"
    if domain:
        line += f"  ({domain})"
    if desc:
        line += f"\n      {desc}"
    return line


def format_timeline(items, cursors=None):
    """Format a list of tweets/notifications as a timeline."""
    parts = []
    for item in items:
        item_type = item.get("type", "")
        if item_type == "notification":
            parts.append(format_notification(item))
        elif item_type == "trend":
            parts.append(format_trend(item))
        else:
            parts.append(format_tweet(item))
        parts.append("")  # blank line between items

    output = "\n".join(parts)

    if cursors and cursors.get("bottom"):
        output += f"\n--- cursor: {cursors['bottom'][:40]}... ---\n"

    return output


