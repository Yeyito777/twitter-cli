"""Reading commands — timeline, tweet, search, profile, etc."""

import argparse
import sys

from src.api import graphql_get, rest_get
from src.parse import parse_timeline_entries, parse_tweet
from src.format import format_timeline, format_tweet, format_json
from src.helpers import (
    Q, DM_PARAMS, require_tweet_ref, compact_num, resolve_user_id,
    format_dm_time,
)


def timeline(argv):
    """Fetch and display the home timeline."""
    p = argparse.ArgumentParser(prog="twitter timeline")
    p.add_argument("-n", "--count", type=int, default=20, help="Number of tweets")
    p.add_argument("-c", "--cursor", type=str, help="Pagination cursor")
    p.add_argument("-l", "--latest", action="store_true", help="Chronological instead of algorithmic")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    args = p.parse_args(argv)

    variables = {
        "count": args.count,
        "includePromotedContent": False,
        "requestContext": "launch",
        "withCommunity": True,
    }
    if args.cursor:
        variables["cursor"] = args.cursor

    op = "HomeLatestTimeline" if args.latest else "HomeTimeline"
    data = graphql_get(Q[op], op, variables)

    entries = data["data"]["home"]["home_timeline_urt"]["instructions"][0]["entries"]
    tweets, cursors = parse_timeline_entries(entries)

    if args.json:
        print(format_json(tweets))
    else:
        print(format_timeline(tweets, cursors))


def tweet(argv):
    """View a single tweet by ID or URL."""
    p = argparse.ArgumentParser(prog="twitter tweet")
    p.add_argument("tweet", help="Tweet ID or URL")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    args = p.parse_args(argv)

    tweet_id = require_tweet_ref(args.tweet)

    variables = {
        "tweetId": tweet_id,
        "withCommunity": True,
        "includePromotedContent": False,
        "withVoice": True,
    }

    data = graphql_get(Q["TweetResultByRestId"], "TweetResultByRestId", variables)
    result = data.get("data", {}).get("tweetResult", {}).get("result")
    parsed = parse_tweet(result)

    if args.json:
        print(format_json(parsed or data))
    elif parsed:
        print(format_tweet(parsed))
    else:
        print("Tweet not found or could not be parsed.")


def search(argv):
    """Search tweets by keyword, hashtag, or advanced query."""
    p = argparse.ArgumentParser(prog="twitter search")
    p.add_argument("query", nargs="+", help="Search query")
    p.add_argument("-n", "--count", type=int, default=20, help="Number of results")
    p.add_argument("-c", "--cursor", type=str, help="Pagination cursor")
    p.add_argument("-l", "--latest", action="store_true", help="Sort by latest")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    args = p.parse_args(argv)

    query = " ".join(args.query)

    variables = {
        "rawQuery": query,
        "count": args.count,
        "querySource": "typed_query",
        "product": "Latest" if args.latest else "Top",
    }
    if args.cursor:
        variables["cursor"] = args.cursor

    data = graphql_get(Q["SearchTimeline"], "SearchTimeline", variables)

    instructions = (
        data.get("data", {})
        .get("search_by_raw_query", {})
        .get("search_timeline", {})
        .get("timeline", {})
        .get("instructions", [])
    )

    entries = []
    for inst in instructions:
        if inst.get("type") == "TimelineAddEntries":
            entries = inst.get("entries", [])
            break

    tweets, cursors = parse_timeline_entries(entries)

    if args.json:
        print(format_json(tweets))
    else:
        if not tweets:
            print(f"No results for: {query}")
        else:
            print(format_timeline(tweets, cursors))


def profile(argv):
    """View a user's profile info."""
    p = argparse.ArgumentParser(prog="twitter profile")
    p.add_argument("user", help="Username (with or without @)")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    args = p.parse_args(argv)

    screen_name = args.user.lstrip("@")

    variables = {"screen_name": screen_name, "withSafetyModeUserFields": True}

    data = graphql_get(Q["UserByScreenName"], "UserByScreenName", variables)
    user_result = data.get("data", {}).get("user", {}).get("result", {})

    if not user_result:
        print(f"User @{screen_name} not found.")
        return

    if args.json:
        print(format_json(user_result))
        return

    user_core = user_result.get("core", {})
    user_legacy = user_result.get("legacy", {})
    bio = (
        user_result.get("profile_bio", {}).get("description", "")
        or user_legacy.get("description", "")
    )
    location = (
        user_result.get("location", {}).get("location", "")
        or user_legacy.get("location", "")
    )

    name = user_core.get("name") or user_legacy.get("name", "?")
    handle = user_core.get("screen_name") or user_legacy.get("screen_name", "?")
    verified = user_result.get("is_blue_verified", False)

    followers = user_legacy.get("followers_count", 0)
    following = user_legacy.get("friends_count", 0)
    tweets_count = user_legacy.get("statuses_count", 0)
    created = user_core.get("created_at", "")

    v_mark = " ✓" if verified else ""
    print(f"@{handle} ({name}){v_mark}")
    if bio:
        print(f"  {bio}")
    if location:
        print(f"  📍 {location}")
    print(f"  Joined: {created}")
    print(
        f"  {compact_num(followers)} followers · "
        f"{compact_num(following)} following · "
        f"{compact_num(tweets_count)} tweets"
    )


def tweets(argv):
    """View a user's tweets."""
    p = argparse.ArgumentParser(prog="twitter tweets")
    p.add_argument("user", help="Username (with or without @)")
    p.add_argument("-n", "--count", type=int, default=20, help="Number of tweets")
    p.add_argument("-c", "--cursor", type=str, help="Pagination cursor")
    p.add_argument("--replies", action="store_true", help="Include replies")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    args = p.parse_args(argv)

    user_id, user_result = resolve_user_id(args.user)

    variables = {
        "userId": user_id,
        "count": args.count,
        "includePromotedContent": False,
        "withQuickPromoteEligibilityTweetFields": True,
        "withVoice": True,
        "withV2Timeline": True,
    }
    if args.cursor:
        variables["cursor"] = args.cursor

    op = "UserTweetsAndReplies" if args.replies else "UserTweets"
    data = graphql_get(Q[op], op, variables)

    instructions = (
        data.get("data", {})
        .get("user", {}).get("result", {})
        .get("timeline", {}).get("timeline", {})
        .get("instructions", [])
    )

    entries = []
    for inst in instructions:
        if inst.get("type") == "TimelineAddEntries":
            entries = inst.get("entries", [])
            break

    items, cursors = parse_timeline_entries(entries)

    if args.json:
        print(format_json(items))
    else:
        if not items:
            print(f"No tweets found for @{args.user.lstrip('@')}")
        else:
            print(format_timeline(items, cursors))


def thread(argv):
    """View a full thread/conversation around a tweet."""
    p = argparse.ArgumentParser(prog="twitter thread")
    p.add_argument("tweet", help="Tweet ID or URL (any tweet in the thread)")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    args = p.parse_args(argv)

    tweet_id = require_tweet_ref(args.tweet)

    variables = {
        "focalTweetId": tweet_id,
        "with_rux_injections": False,
        "includePromotedContent": False,
        "withCommunity": True,
        "withQuickPromoteEligibilityTweetFields": True,
        "withBirdwatchNotes": True,
        "withVoice": True,
        "withV2Timeline": True,
    }

    data = graphql_get(Q["TweetDetail"], "TweetDetail", variables)

    instructions = (
        data.get("data", {})
        .get("threaded_conversation_with_injections_v2", {})
        .get("instructions", [])
    )

    entries = []
    for inst in instructions:
        if inst.get("type") == "TimelineAddEntries":
            entries = inst.get("entries", [])
            break

    items, cursors = parse_timeline_entries(entries)

    if args.json:
        print(format_json(items))
    else:
        if not items:
            print("Thread not found or could not be parsed.")
        else:
            print(format_timeline(items, cursors))


def notifications(argv):
    """View recent notifications."""
    p = argparse.ArgumentParser(prog="twitter notifications")
    p.add_argument("-n", "--count", type=int, default=20, help="Number of notifications")
    p.add_argument("-c", "--cursor", type=str, help="Pagination cursor")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    args = p.parse_args(argv)

    variables = {
        "timeline_type": "All",
        "count": args.count,
    }
    if args.cursor:
        variables["cursor"] = args.cursor

    data = graphql_get(
        Q["NotificationsTimeline"], "NotificationsTimeline", variables,
    )

    instructions = (
        data.get("data", {})
        .get("viewer_v2", {}).get("user_results", {}).get("result", {})
        .get("notification_timeline", {}).get("timeline", {})
        .get("instructions", [])
    )

    entries = []
    for inst in instructions:
        if inst.get("type") == "TimelineAddEntries":
            entries = inst.get("entries", [])
            break

    items, cursors = parse_timeline_entries(entries)

    if args.json:
        print(format_json(items))
    else:
        if not items:
            print("No notifications found.")
        else:
            print(format_timeline(items, cursors))


def bookmarks(argv):
    """List bookmarked tweets."""
    p = argparse.ArgumentParser(prog="twitter bookmarks")
    p.add_argument("-n", "--count", type=int, default=20, help="Number of bookmarks")
    p.add_argument("-c", "--cursor", type=str, help="Pagination cursor")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    args = p.parse_args(argv)

    variables = {
        "count": args.count,
        "includePromotedContent": False,
    }
    if args.cursor:
        variables["cursor"] = args.cursor

    data = graphql_get(Q["Bookmarks"], "Bookmarks", variables)

    instructions = (
        data.get("data", {})
        .get("bookmark_timeline_v2", {}).get("timeline", {})
        .get("instructions", [])
    )

    entries = []
    for inst in instructions:
        if inst.get("type") == "TimelineAddEntries":
            entries = inst.get("entries", [])
            break

    items, cursors = parse_timeline_entries(entries)

    if args.json:
        print(format_json(items))
    else:
        if not items:
            print("No bookmarks found.")
        else:
            print(format_timeline(items, cursors))


def trending(argv):
    """View trending topics."""
    p = argparse.ArgumentParser(prog="twitter trending")
    p.add_argument("-n", "--count", type=int, default=20, help="Number of trends")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    args = p.parse_args(argv)

    # Step 1: fetch ExplorePage to get the trending timeline ID
    data = graphql_get(
        Q["ExplorePage"], "ExplorePage",
        {"count": 20, "includePromotedContent": False},
    )

    body = data.get("data", {}).get("explore_page", {}).get("body", {})
    timelines = body.get("timelines", [])

    trending_id = None
    for t in timelines:
        if t.get("id") == "trending":
            trending_id = t.get("timeline", {}).get("id")
            break

    if not trending_id:
        print("Error: could not find trending timeline.", file=sys.stderr)
        sys.exit(1)

    # Step 2: fetch the trending timeline
    data = graphql_get(
        Q["GenericTimelineById"], "GenericTimelineById",
        {
            "timelineId": trending_id,
            "count": args.count,
            "withQuickPromoteEligibilityTweetFields": True,
        },
    )

    instructions = (
        data.get("data", {})
        .get("timeline", {}).get("timeline", {})
        .get("instructions", [])
    )

    entries = []
    for inst in instructions:
        if inst.get("type") == "TimelineAddEntries":
            entries = inst.get("entries", [])
            break

    items, cursors = parse_timeline_entries(entries)

    if args.json:
        print(format_json(items))
    else:
        if not items:
            print("No trending topics found.")
        else:
            print(format_timeline(items, cursors))


def dms(argv):
    """List DM conversations."""
    p = argparse.ArgumentParser(prog="twitter dms")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    args = p.parse_args(argv)

    data = rest_get("/1.1/dm/inbox_initial_state.json", DM_PARAMS)
    inbox = data.get("inbox_initial_state", {})
    users = inbox.get("users", {})
    convos = inbox.get("conversations", {})
    entries = inbox.get("entries", [])

    # Build user map
    user_map = {}
    for uid, u in users.items():
        user_map[uid] = {
            "name": u.get("name", uid),
            "handle": u.get("screen_name", uid),
        }

    # Build last message per conversation
    last_msg = {}
    for entry in entries:
        msg = entry.get("message", {})
        conv_id = msg.get("conversation_id", "")
        md = msg.get("message_data", {})
        time_ms = msg.get("time", "0")
        if conv_id not in last_msg or int(time_ms) > int(last_msg[conv_id]["time"]):
            last_msg[conv_id] = {
                "sender": md.get("sender_id", ""),
                "text": md.get("text", ""),
                "time": time_ms,
            }

    results = []
    for conv_id, conv in convos.items():
        participants = conv.get("participants", [])
        others = []
        for pt in participants:
            uid = pt["user_id"]
            if uid in user_map:
                others.append(user_map[uid])
        lm = last_msg.get(conv_id, {})
        results.append({
            "conversation_id": conv_id,
            "participants": others,
            "last_message": lm.get("text", ""),
            "last_sender": user_map.get(lm.get("sender", ""), {}).get("handle", "?"),
            "last_time": format_dm_time(lm.get("time", "")),
            "type": conv.get("type", ""),
        })

    if args.json:
        print(format_json(results))
    else:
        if not results:
            print("No conversations found.")
        else:
            for r in results:
                names = " & ".join(
                    f"@{pt['handle']} ({pt['name']})" for pt in r["participants"]
                )
                print(f"💬 {names}")
                print(f"   ID: {r['conversation_id']}")
                if r["last_message"]:
                    preview = r["last_message"][:100]
                    if len(r["last_message"]) > 100:
                        preview += "..."
                    print(f"   [{r['last_time']}] @{r['last_sender']}: {preview}")
                print()


def dm(argv):
    """Read or send a DM in a conversation."""
    p = argparse.ArgumentParser(prog="twitter dm")
    p.add_argument("conversation", help="Conversation ID or @username")
    p.add_argument("-s", "--send", nargs="+", help="Send a message")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    args = p.parse_args(argv)

    # If --send is provided, send a message
    if args.send:
        _dm_send(args)
        return

    # Otherwise, read the conversation
    conv_id = _resolve_dm_conv_id(args.conversation)

    data = rest_get(f"/1.1/dm/conversation/{conv_id}.json", DM_PARAMS)
    conv = data.get("conversation_timeline", {})
    entries = conv.get("entries", [])
    users = conv.get("users", {})

    user_map = {}
    for uid, u in users.items():
        user_map[uid] = u.get("screen_name", uid)

    messages = []
    for entry in entries:
        msg = entry.get("message", {})
        md = msg.get("message_data", {})
        if not md:
            continue
        sender_id = md.get("sender_id", "")
        messages.append({
            "sender": user_map.get(sender_id, sender_id),
            "text": md.get("text", ""),
            "time": format_dm_time(msg.get("time", "")),
            "id": entry.get("message", {}).get("id", ""),
        })

    if args.json:
        print(format_json(messages))
    else:
        if not messages:
            print("No messages in this conversation.")
        else:
            for m in messages:
                print(f"[{m['time']}] @{m['sender']}: {m['text']}")


def _resolve_dm_conv_id(ref):
    """Resolve a @username or conversation ID to a conversation ID."""
    from src.api import rest_get as _rest_get

    if ref.replace("-", "").isdigit():
        return ref

    user_id, _ = resolve_user_id(ref)
    data = _rest_get("/1.1/dm/inbox_initial_state.json", DM_PARAMS)
    inbox = data.get("inbox_initial_state", {})
    convos = inbox.get("conversations", {})
    for cid, conv in convos.items():
        pids = [pt["user_id"] for pt in conv.get("participants", [])]
        if user_id in pids:
            return cid

    print(f"No conversation found with @{ref.lstrip('@')}", file=sys.stderr)
    sys.exit(1)


def _dm_send(args):
    """Send a DM."""
    from src.api import rest_post

    conv_id = args.conversation
    text = " ".join(args.send)

    # If a @username is given, resolve to conversation ID
    if not conv_id.replace("-", "").isdigit():
        user_id, _ = resolve_user_id(conv_id)
        data = rest_get("/1.1/dm/inbox_initial_state.json", DM_PARAMS)
        inbox = data.get("inbox_initial_state", {})
        convos = inbox.get("conversations", {})
        found = False
        for cid, conv in convos.items():
            pids = [pt["user_id"] for pt in conv.get("participants", [])]
            if user_id in pids:
                conv_id = cid
                found = True
                break
        if not found:
            # New conversation to a user
            payload = {
                "recipient_ids": [user_id],
                "text": text,
                "cards_platform": "Web-12",
                "include_cards": 1,
            }
            result = rest_post("/1.1/dm/new2.json", payload)
            if args.json:
                print(format_json(result))
            else:
                print("DM sent.")
            return

    payload = {
        "conversation_id": conv_id,
        "text": text,
        "cards_platform": "Web-12",
        "include_cards": 1,
        "include_quote_count": True,
        "dm_users": False,
    }

    result = rest_post("/1.1/dm/new2.json", payload)

    if args.json:
        print(format_json(result))
    else:
        print(f"DM sent.")
