"""Parse Twitter API response structures into clean data."""

from datetime import datetime


def parse_tweet(result):
    """Parse a tweet result object into a flat dict.

    Handles both regular Tweet and TweetWithVisibilityResults wrappers.
    Returns None if the result can't be parsed.
    """
    if not result:
        return None

    typename = result.get("__typename", "")

    # Unwrap visibility wrapper
    if typename == "TweetWithVisibilityResults":
        result = result.get("tweet", {})
    elif typename and typename not in ("Tweet",):
        # Unknown wrapper type — skip non-tweet objects but allow missing __typename
        return None

    legacy = result.get("legacy", {})
    if not legacy:
        return None

    # User info — check both core and legacy locations
    user_result = (
        result.get("core", {}).get("user_results", {}).get("result", {})
    )
    user_core = user_result.get("core", {})
    user_legacy = user_result.get("legacy", {})

    name = user_core.get("name") or user_legacy.get("name", "?")
    handle = user_core.get("screen_name") or user_legacy.get("screen_name", "?")

    # Tweet metadata
    tweet_id = result.get("rest_id", "")
    created_at = _parse_date(legacy.get("created_at", ""))
    full_text = legacy.get("full_text", "")
    lang = legacy.get("lang", "")

    # Engagement
    likes = legacy.get("favorite_count", 0)
    retweets = legacy.get("retweet_count", 0)
    replies = legacy.get("reply_count", 0)
    quotes = legacy.get("quote_count", 0)
    bookmarks = legacy.get("bookmark_count", 0)
    views = result.get("views", {}).get("count", "0")

    # Reply context
    in_reply_to = legacy.get("in_reply_to_screen_name")
    in_reply_to_id = legacy.get("in_reply_to_status_id_str")

    # Retweet detection
    retweeted_status = legacy.get("retweeted_status_result", {}).get("result")
    is_retweet = retweeted_status is not None

    # Quote tweet detection
    is_quote = legacy.get("is_quote_status", False)
    quoted_status = result.get("quoted_status_result", {}).get("result")

    # Media
    media_list = []
    extended = legacy.get("extended_entities", {}) or legacy.get("entities", {})
    for m in extended.get("media", []):
        media_list.append({
            "type": m.get("type", "photo"),
            "url": m.get("media_url_https", m.get("url", "")),
            "expanded_url": m.get("expanded_url", ""),
        })

    tweet = {
        "id": tweet_id,
        "name": name,
        "handle": handle,
        "text": full_text,
        "created_at": created_at,
        "lang": lang,
        "likes": likes,
        "retweets": retweets,
        "replies": replies,
        "quotes": quotes,
        "bookmarks": bookmarks,
        "views": views,
        "is_retweet": is_retweet,
        "is_quote": is_quote,
        "is_reply": in_reply_to is not None,
        "in_reply_to": in_reply_to,
        "in_reply_to_id": in_reply_to_id,
        "media": media_list,
        "url": f"https://x.com/{handle}/status/{tweet_id}",
    }

    # If it's a retweet, include the original tweet
    if is_retweet and retweeted_status:
        tweet["retweeted"] = parse_tweet(retweeted_status)

    # If it's a quote tweet, include the quoted tweet
    if is_quote and quoted_status:
        tweet["quoted"] = parse_tweet(quoted_status)

    return tweet


def parse_notification(item):
    """Parse a TimelineNotification into a flat dict.

    Returns None if the item can't be parsed.
    """
    if not item or item.get("__typename") != "TimelineNotification":
        return None

    message = item.get("rich_message", {}).get("text", "")
    url_info = item.get("notification_url", {})
    url = url_info.get("url", "")
    icon = item.get("notification_icon", "")
    timestamp_ms = item.get("timestamp_ms", "")

    # Parse timestamp
    created_at = ""
    if timestamp_ms:
        try:
            from datetime import datetime, timezone
            dt = datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=timezone.utc)
            created_at = dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError, OSError):
            pass

    # Map icon names to emoji
    icon_map = {
        "heart_icon": "♥",
        "retweet_icon": "🔁",
        "person_icon": "👤",
        "recommendation_icon": "📌",
        "reply_icon": "💬",
        "bell_icon": "🔔",
    }
    icon_emoji = icon_map.get(icon, "•")

    return {
        "type": "notification",
        "icon": icon_emoji,
        "icon_name": icon,
        "message": message,
        "url": url,
        "created_at": created_at,
        "id": item.get("id", ""),
    }


def parse_trend(item):
    """Parse a TimelineTrend into a flat dict.

    Returns None if the item can't be parsed.
    """
    if not item or item.get("__typename") != "TimelineTrend":
        return None

    meta = item.get("trend_metadata", {})
    return {
        "type": "trend",
        "name": item.get("name", "?"),
        "rank": item.get("rank", ""),
        "domain": meta.get("domain_context", ""),
        "description": meta.get("meta_description", ""),
    }


def parse_timeline_entries(entries):
    """Parse timeline entries into a list of items and cursor info.

    Handles tweets (TimelineTweet) and notifications (TimelineNotification).
    Returns (items, cursors) where cursors is a dict with 'top' and 'bottom'.
    """
    items = []
    cursors = {}

    for entry in entries:
        content = entry.get("content", {})
        typename = content.get("__typename", "")

        if typename == "TimelineTimelineItem":
            item = content.get("itemContent", {})
            item_type = item.get("__typename", "")

            if item_type == "TimelineTweet":
                result = item.get("tweet_results", {}).get("result")
                tweet = parse_tweet(result)
                if tweet:
                    items.append(tweet)

            elif item_type == "TimelineNotification":
                notif = parse_notification(item)
                if notif:
                    items.append(notif)

            elif item_type == "TimelineTrend":
                trend = parse_trend(item)
                if trend:
                    items.append(trend)

        elif typename == "TimelineTimelineCursor":
            cursor_type = content.get("cursorType", "")
            if cursor_type in ("Top", "Bottom"):
                cursors[cursor_type.lower()] = content.get("value", "")

        elif typename == "TimelineTimelineModule":
            # Conversation threads, promoted content clusters
            sub_items = content.get("items", [])
            for item_wrapper in sub_items:
                item = item_wrapper.get("item", {}).get("itemContent", {})
                item_type = item.get("__typename", "")

                if item_type == "TimelineTweet":
                    result = item.get("tweet_results", {}).get("result")
                    tweet = parse_tweet(result)
                    if tweet:
                        items.append(tweet)

                elif item_type == "TimelineNotification":
                    notif = parse_notification(item)
                    if notif:
                        items.append(notif)

    return items, cursors


def _parse_date(date_str):
    """Parse Twitter's date format into ISO format."""
    if not date_str:
        return ""
    try:
        dt = datetime.strptime(date_str, "%a %b %d %H:%M:%S %z %Y")
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return date_str
