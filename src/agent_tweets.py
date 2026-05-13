"""Track tweets authored through the twitter CLI/agents.

The notification relay uses this to avoid taking over conversations the user
started manually in the browser/phone. Any tweet successfully created through
`twitter post` / `twitter reply` is recorded here, and replies/quotes are only
relayed when they target one of these managed tweet IDs.
"""

import json
import time
from pathlib import Path

from src.auth import PROJECT_ROOT
from src.helpers import parse_tweet_ref

CONFIG_DIR = PROJECT_ROOT / "config"
AGENT_TWEETS_FILE = CONFIG_DIR / "agent-tweets.json"
MAX_MANAGED_TWEETS = 5000


def _dedupe_tail(items, limit):
    seen = set()
    out = []
    for item in reversed(list(items or [])):
        item = str(item).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return list(reversed(out))[-limit:]


def _load_raw():
    if not AGENT_TWEETS_FILE.exists():
        return {}
    try:
        data = json.loads(AGENT_TWEETS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def load_agent_tweets():
    data = _load_raw()
    managed = _dedupe_tail(data.get("managed_tweet_ids", []), MAX_MANAGED_TWEETS)
    details = data.get("tweets", {})
    if not isinstance(details, dict):
        details = {}

    # Keep details only for managed IDs and normalize keys/values.
    managed_set = set(managed)
    normalized_details = {}
    for tweet_id, entry in details.items():
        tweet_id = str(tweet_id).strip()
        if not tweet_id or tweet_id not in managed_set:
            continue
        normalized_details[tweet_id] = entry if isinstance(entry, dict) else {}

    return {
        "managed_tweet_ids": managed,
        "tweets": normalized_details,
        "updated_at": data.get("updated_at", ""),
    }


def save_agent_tweets(data):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data["managed_tweet_ids"] = _dedupe_tail(data.get("managed_tweet_ids", []), MAX_MANAGED_TWEETS)
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    tmp = AGENT_TWEETS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(AGENT_TWEETS_FILE)


def record_agent_tweet(tweet_id, *, text="", command="post", reply_to_id=None, quote_id=None, url=""):
    tweet_id = parse_tweet_ref(str(tweet_id or ""))
    if not tweet_id:
        return False

    data = load_agent_tweets()
    ids = data.setdefault("managed_tweet_ids", [])
    ids.append(tweet_id)
    data["managed_tweet_ids"] = _dedupe_tail(ids, MAX_MANAGED_TWEETS)

    entry = {
        "id": tweet_id,
        "text": text or "",
        "command": command or "post",
        "reply_to_id": parse_tweet_ref(str(reply_to_id or "")) if reply_to_id else None,
        "quote_id": parse_tweet_ref(str(quote_id or "")) if quote_id else None,
        "url": url or f"https://x.com/i/status/{tweet_id}",
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    data.setdefault("tweets", {})[tweet_id] = entry
    save_agent_tweets(data)
    return True


def unrecord_agent_tweet(tweet_id):
    tweet_id = parse_tweet_ref(str(tweet_id or ""))
    if not tweet_id:
        return False
    data = load_agent_tweets()
    before = len(data.get("managed_tweet_ids", []))
    data["managed_tweet_ids"] = [tid for tid in data.get("managed_tweet_ids", []) if tid != tweet_id]
    data.setdefault("tweets", {}).pop(tweet_id, None)
    changed = len(data.get("managed_tweet_ids", [])) != before
    if changed:
        save_agent_tweets(data)
    return changed


def is_agent_tweet(tweet_id):
    tweet_id = parse_tweet_ref(str(tweet_id or ""))
    if not tweet_id:
        return False
    return tweet_id in set(load_agent_tweets().get("managed_tweet_ids", []))


def list_agent_tweets():
    data = load_agent_tweets()
    ids = data.get("managed_tweet_ids", [])
    details = data.get("tweets", {})
    return ids, details
