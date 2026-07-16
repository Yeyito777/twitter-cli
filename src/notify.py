"""Manage Twitter reply/quote publications and Exocortex subscriptions."""

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

from src.api import graphql_get, rest_get
from src.agent_tweets import is_agent_tweet, list_agent_tweets, record_agent_tweet, unrecord_agent_tweet
from src.auth import PROJECT_ROOT
from src.exocortex import (
    list_external_notification_subscriptions,
    manage_external_tool_daemon,
    publish_external_notification,
    register_external_notification_source,
    subscribe_external_notification,
    unsubscribe_external_notification,
)
from src.format import format_tweet
from src.helpers import Q, parse_tweet_ref
from src.parse import parse_timeline_entries, parse_tweet

CONFIG_DIR = PROJECT_ROOT / "config"
CONFIG_FILE = CONFIG_DIR / "notify.json"
STATE_FILE = CONFIG_DIR / "notify-state.json"
LISTENER_DIR = Path("/tmp/twitter-listeners")
LOCK_FILE = LISTENER_DIR / "__notify__.lock"

DEFAULT_POLL_SECONDS = 300
DEFAULT_MAX_PARENT_REPLIES = 10  # exclusive upper bound: relay when parent replies < 10
DEFAULT_FETCH_COUNT = 50
MAX_SEEN_IDS = 1000
MAX_RELAYED_IDS = 1000
MAX_PENDING_EVENTS = 200

TOOL_NAME = "twitter"
NOTIFICATION_SOURCE_ID = "managed-tweet-replies"
NOTIFICATION_SOURCE_LABEL = "Replies and quotes to agent-managed tweets"


def _read_json_file(path, default=None):
    if default is None:
        default = {}
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return dict(default)
    return dict(default)


def _listener_paths():
    return {
        "pid": LISTENER_DIR / "__notify__.pid",
        "log": LISTENER_DIR / "__notify__.log",
        "err": LISTENER_DIR / "__notify__.err",
        "meta": LISTENER_DIR / "__notify__.meta",
    }


def _load_config():
    cfg = _read_json_file(CONFIG_FILE)

    normalized = {
        "poll_seconds": int(cfg.get("poll_seconds", DEFAULT_POLL_SECONDS)),
        "max_parent_replies": int(cfg.get("max_parent_replies", DEFAULT_MAX_PARENT_REPLIES)),
    }
    # Kept only until daemon-start migration has successfully imported every
    # legacy target into the core subscription registry.
    if "relay_targets" in cfg:
        normalized["relay_targets"] = list(cfg.get("relay_targets") or [])
    return normalized


def _save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp_file = CONFIG_FILE.with_suffix(".tmp")
    tmp_file.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp_file.replace(CONFIG_FILE)


def _dedupe_tail(items, limit):
    seen = set()
    out = []
    for item in reversed(list(items or [])):
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return list(reversed(out))[-limit:]


def _normalize_relay_map(raw):
    result = {}
    if not isinstance(raw, dict):
        return result
    for conv_id, tweet_ids in raw.items():
        if not conv_id:
            continue
        result[str(conv_id)] = _dedupe_tail([str(t) for t in (tweet_ids or []) if t], MAX_RELAYED_IDS)
    return result


def _normalize_pending_events(raw):
    result = {}
    if not isinstance(raw, dict):
        return result
    for tweet_id, event in list(raw.items())[-MAX_PENDING_EVENTS:]:
        if not tweet_id or not isinstance(event, dict):
            continue
        result[str(tweet_id)] = event
    return result


def _load_state():
    state = _read_json_file(STATE_FILE)

    return {
        "initialized": bool(state.get("initialized", False)),
        "seen_entry_ids": _dedupe_tail(state.get("seen_entry_ids", []), MAX_SEEN_IDS),
        "relayed_tweet_ids": _dedupe_tail(state.get("relayed_tweet_ids", []), MAX_RELAYED_IDS),
        "relayed_by_target": _normalize_relay_map(state.get("relayed_by_target", {})),
        "pending_events": _normalize_pending_events(state.get("pending_events", {})),
        "last_poll": state.get("last_poll", ""),
    }


def _save_state(state):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    state = dict(state)
    state["seen_entry_ids"] = _dedupe_tail(state.get("seen_entry_ids", []), MAX_SEEN_IDS)
    state["relayed_tweet_ids"] = _dedupe_tail(state.get("relayed_tweet_ids", []), MAX_RELAYED_IDS)
    state["relayed_by_target"] = _normalize_relay_map(state.get("relayed_by_target", {}))
    state["pending_events"] = _normalize_pending_events(state.get("pending_events", {}))

    tmp_file = STATE_FILE.with_suffix(".tmp")
    tmp_file.write_text(json.dumps(state, indent=2) + "\n")
    tmp_file.replace(STATE_FILE)


def get_relay_targets():
    """Return not-yet-migrated legacy relay targets."""
    return _load_config().get("relay_targets", [])


def _migrate_legacy_relay_targets():
    """Import old config routes exactly once, removing them only on success."""
    cfg = _read_json_file(CONFIG_FILE)
    if "relay_targets" not in cfg:
        return 0

    targets = _dedupe_tail(
        [str(target) for target in (cfg.get("relay_targets") or []) if target],
        MAX_RELAYED_IDS,
    )
    subscriptions = list_external_notification_subscriptions(
        tool_name=TOOL_NAME,
        source_id=NOTIFICATION_SOURCE_ID,
    )
    existing_conv_ids = {
        str(subscription.get("convId"))
        for subscription in subscriptions
        if subscription.get("convId")
    }

    # subscribe_external_notification is an upsert in core, while the list
    # check also preserves an existing route's chosen delivery mode.
    for conv_id in targets:
        if conv_id in existing_conv_ids:
            continue
        subscribe_external_notification(
            TOOL_NAME,
            NOTIFICATION_SOURCE_ID,
            conv_id,
            delivery="wake",
            source_label=NOTIFICATION_SOURCE_LABEL,
        )

    # Re-read before writing so unrelated poll/filter edits made during IPC
    # calls are retained. A newly changed legacy list is left for the next
    # startup rather than accidentally marking it migrated.
    latest = _read_json_file(CONFIG_FILE)
    if latest.get("relay_targets") != cfg.get("relay_targets"):
        raise RuntimeError("notify.json relay_targets changed during migration")
    latest.pop("relay_targets", None)
    _save_config(latest)
    return len(targets)


def _find_notify_pids():
    try:
        result = subprocess.run(
            ["pgrep", "-f", r"src\.notify\s+__run__"],
            capture_output=True,
            text=True,
        )
        return sorted({int(p) for p in result.stdout.strip().split() if p.strip()})
    except Exception:
        return []


def _find_notify_pid():
    pids = _find_notify_pids()
    return pids[0] if pids else None


def _stop_notify_pids(pids, *, verbose=True):
    stopped = []
    already_stopped = []
    for pid in sorted(set(pids)):
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(10):
                time.sleep(0.5)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
            else:
                os.kill(pid, signal.SIGKILL)
            stopped.append(pid)
            if verbose:
                print(f"  Stopped notify listener (PID {pid})")
        except ProcessLookupError:
            already_stopped.append(pid)
            if verbose:
                print(f"  Notify listener already stopped (PID {pid})")
    return stopped, already_stopped


class TwitterNotifyService:
    def __init__(self, log_file):
        self.log_file = Path(log_file)
        self.running = True
        self.cfg = _load_config()
        self.poll_seconds = max(30, int(self.cfg.get("poll_seconds", DEFAULT_POLL_SECONDS)))
        self.max_parent_replies = max(1, int(self.cfg.get("max_parent_replies", DEFAULT_MAX_PARENT_REPLIES)))
        self.fetch_count = DEFAULT_FETCH_COUNT
        self.state = _load_state()
        self.state.setdefault("relayed_by_target", {})
        self.state.setdefault("pending_events", {})
        self.self_profile = self._get_self_profile()
        self.self_handle = self.self_profile.get("screen_name", "").lstrip("@").lower()
        self.self_name = self.self_profile.get("name", self.self_handle)
        self.lock_handle = None

        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

    def _acquire_lock(self):
        LISTENER_DIR.mkdir(parents=True, exist_ok=True)
        self.lock_handle = LOCK_FILE.open("a+")
        try:
            fcntl.flock(self.lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            owner = ""
            try:
                self.lock_handle.seek(0)
                owner = self.lock_handle.read().strip()
            except OSError:
                owner = ""
            self._log(
                "Another notify service already holds the listener lock"
                f"{f' ({owner})' if owner else ''}; exiting"
            )
            try:
                self.lock_handle.close()
            except OSError:
                pass
            self.lock_handle = None
            return False

        self.lock_handle.seek(0)
        self.lock_handle.truncate()
        self.lock_handle.write(str(os.getpid()))
        self.lock_handle.flush()
        return True

    def _release_lock(self):
        if not self.lock_handle:
            return
        try:
            self.lock_handle.seek(0)
            self.lock_handle.truncate()
            self.lock_handle.flush()
            fcntl.flock(self.lock_handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self.lock_handle.close()
        except OSError:
            pass
        self.lock_handle = None

    def _shutdown(self, signum=None, frame=None):
        sig_name = signal.Signals(signum).name if signum else "?"
        self._log(f"Received {sig_name}, shutting down")
        self.running = False

    def _log(self, msg):
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with self.log_file.open("a") as f:
            f.write(f"[{ts}] {msg}\n")

    def _sleep(self, seconds):
        deadline = time.time() + seconds
        while self.running and time.time() < deadline:
            time.sleep(min(1.0, deadline - time.time()))

    def _get_self_profile(self):
        data = rest_get("/1.1/account/verify_credentials.json")
        return {
            "id": data.get("id_str") or str(data.get("id", "")),
            "screen_name": data.get("screen_name", ""),
            "name": data.get("name", ""),
        }

    def _fetch_notification_entries(self):
        variables = {
            "timeline_type": "All",
            "count": self.fetch_count,
        }
        data = graphql_get(Q["NotificationsTimeline"], "NotificationsTimeline", variables)
        instructions = (
            data.get("data", {})
            .get("viewer_v2", {}).get("user_results", {}).get("result", {})
            .get("notification_timeline", {}).get("timeline", {})
            .get("instructions", [])
        )

        for inst in instructions:
            if inst.get("type") == "TimelineAddEntries":
                return inst.get("entries", [])
        return []

    def _fetch_tweet(self, tweet_ref):
        tweet_id = parse_tweet_ref(tweet_ref)
        if not tweet_id:
            return None

        variables = {
            "tweetId": tweet_id,
            "withCommunity": True,
            "includePromotedContent": False,
            "withVoice": True,
        }
        data = graphql_get(Q["TweetResultByRestId"], "TweetResultByRestId", variables)
        result = data.get("data", {}).get("tweetResult", {}).get("result")
        return parse_tweet(result)

    def _fetch_thread_items(self, tweet_id):
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

        items, _ = parse_timeline_entries(entries)
        return [item for item in items if item.get("type") not in ("notification", "trend")]

    def _format_thread_snapshot(self, items, focal_tweet_id):
        if not items:
            return "(no extra thread context available)"

        idx = next((i for i, item in enumerate(items) if item.get("id") == focal_tweet_id), None)
        if idx is None:
            window = items[:4]
        else:
            start = max(0, idx - 2)
            end = min(len(items), idx + 3)
            window = items[start:end]

        blocks = []
        for item in window:
            prefix = "[incoming] " if item.get("id") == focal_tweet_id else ""
            blocks.append(prefix + format_tweet(item))
        return "\n\n".join(blocks)

    def _build_event(self, event_type, source, incoming_tweet, parent_tweet, reason, *, thread_items=None):
        return {
            "event_type": event_type,
            "source": source,
            "incoming_tweet": incoming_tweet,
            "parent_tweet": parent_tweet,
            "thread_items": list(thread_items or []),
            "reason": reason,
            "priority": "high",
        }

    def _classify_tweet(self, tweet, source="tweet_item"):
        if not tweet:
            return None
        if tweet.get("handle", "").lower() == self.self_handle:
            return None

        if tweet.get("is_reply") and tweet.get("in_reply_to_id"):
            parent = self._fetch_tweet(tweet.get("in_reply_to_id"))
            if parent and parent.get("handle", "").lower() == self.self_handle:
                if not is_agent_tweet(parent.get("id")):
                    self._log(
                        f"Skip @{tweet['handle']} reply {tweet['id']}: "
                        f"parent {parent.get('id')} is not agent-managed"
                    )
                    return None
                parent_replies = int(parent.get("replies", 0) or 0)
                if parent_replies < self.max_parent_replies:
                    thread_items = self._fetch_thread_items(tweet["id"])
                    reason = (
                        f"Reply to your tweet, parent currently has "
                        f"{parent_replies} replies (threshold: <{self.max_parent_replies})"
                    )
                    return self._build_event(
                        "direct_reply",
                        source,
                        tweet,
                        parent,
                        reason,
                        thread_items=thread_items,
                    )
                self._log(
                    f"Skip @{tweet['handle']} reply {tweet['id']}: "
                    f"parent already has {parent_replies} replies (threshold <{self.max_parent_replies})"
                )
                return None

        if tweet.get("is_quote") and tweet.get("quoted"):
            quoted = tweet.get("quoted")
            if quoted and quoted.get("handle", "").lower() == self.self_handle:
                if not is_agent_tweet(quoted.get("id")):
                    self._log(
                        f"Skip @{tweet['handle']} quote {tweet['id']}: "
                        f"quoted tweet {quoted.get('id')} is not agent-managed"
                    )
                    return None
                return self._build_event(
                    "quote_tweet",
                    source,
                    tweet,
                    quoted,
                    "Someone quote-tweeted one of your tweets",
                )

        return None

    def _classify_notification_fallback(self, item):
        icon = item.get("notification_icon", "")
        message = item.get("rich_message", {}).get("text", "")
        url = item.get("notification_url", {}).get("url", "")
        tweet_id = parse_tweet_ref(url)
        if not tweet_id:
            return None

        lower = message.lower()
        if icon != "reply_icon" and "quoted your" not in lower and "replied to your" not in lower:
            return None

        tweet = self._fetch_tweet(tweet_id)
        return self._classify_tweet(tweet, source="notification_fallback")

    def _extract_event_from_entry(self, entry):
        content = entry.get("content", {})
        if content.get("__typename") != "TimelineTimelineItem":
            return None

        item = content.get("itemContent", {})
        item_type = item.get("__typename")
        if item_type == "TimelineTweet":
            tweet = parse_tweet(item.get("tweet_results", {}).get("result"))
            return self._classify_tweet(tweet)
        if item_type == "TimelineNotification":
            return self._classify_notification_fallback(item)
        return None

    def _format_event_message(self, event):
        incoming = event["incoming_tweet"]
        parent = event["parent_tweet"]

        lines = [
            "[Twitter Reply Notification]",
            "",
            f"Type: {event['event_type']}",
            f"Priority: {event['priority']}",
            f"Why this was relayed: {event['reason']}",
            f"Source: {event['source']}",
            "",
            "Author:",
            f"- @{incoming['handle']} ({incoming['name']})",
            "",
            "Incoming tweet:",
            self._indent(format_tweet(incoming)),
            "",
        ]

        if event["event_type"] == "direct_reply":
            lines.extend([
                "Your tweet being replied to:",
                self._indent(format_tweet(parent)),
                "",
                "Conversation snapshot:",
                self._indent(self._format_thread_snapshot(event.get("thread_items", []), incoming["id"])),
                "",
            ])
        else:
            lines.extend([
                "Your tweet being quoted:",
                self._indent(format_tweet(parent)),
                "",
            ])

        lines.extend([
            "Reply-agent task:",
            "- Decide whether to reply, like, follow, mute, or ignore.",
            "- Prefer fast, tasteful engagement.",
            "- Do not force a reply if the signal is weak.",
            "- You may inspect the thread briefly if needed.",
        ])

        return "\n".join(lines).rstrip() + "\n"

    def _indent(self, text, prefix="  "):
        return "\n".join(prefix + line if line else prefix.rstrip() for line in text.splitlines())

    def _publish_event(self, event):
        msg = self._format_event_message(event)
        incoming_id = event["incoming_tweet"]["id"]
        result = publish_external_notification(
            TOOL_NAME,
            NOTIFICATION_SOURCE_ID,
            incoming_id,
            msg,
        )
        deliveries = list(result.get("deliveries") or [])
        status_counts = {}
        for delivery in deliveries:
            status = delivery.get("status", "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
        summary = ", ".join(
            f"{count} {status}" for status, count in sorted(status_counts.items())
        ) or "no subscribers"
        self._log(f"Published tweet {incoming_id} to Exocortex core: {summary}")
        return result

    def _mark_fully_relayed(self, incoming_id):
        self.state.setdefault("relayed_tweet_ids", []).append(incoming_id)
        self.state.setdefault("pending_events", {}).pop(incoming_id, None)

    def _attempt_event_publish(self, event):
        incoming_id = event["incoming_tweet"].get("id")
        if not incoming_id:
            return False

        if incoming_id in set(self.state.get("relayed_tweet_ids", [])):
            self.state.setdefault("pending_events", {}).pop(incoming_id, None)
            return False

        try:
            result = self._publish_event(event)
        except Exception as e:
            self._log(f"Publish error for tweet {incoming_id}: {e}")
            self.state.setdefault("pending_events", {})[incoming_id] = event
            return False

        failed_deliveries = [
            delivery
            for delivery in (result.get("deliveries") or [])
            if delivery.get("status") == "failed"
        ]
        if failed_deliveries:
            self._log(
                f"Publish for tweet {incoming_id} had {len(failed_deliveries)} failed "
                "delivery target(s); retaining for retry"
            )
            self.state.setdefault("pending_events", {})[incoming_id] = event
            return False

        self._mark_fully_relayed(incoming_id)
        return True

    def poll_once(self):
        entries = self._fetch_notification_entries()
        entry_ids = [e.get("entryId") for e in entries if e.get("entryId")]

        if not self.state.get("initialized"):
            self.state["initialized"] = True
            self.state["seen_entry_ids"] = entry_ids[-MAX_SEEN_IDS:]
            self.state["last_poll"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            _save_state(self.state)
            self._log(f"Primed state with {len(entry_ids)} existing notifications; no relays sent")
            return {"primed": len(entry_ids), "relayed": 0, "new_entries": 0, "pending": 0}

        seen_entry_ids = set(self.state.get("seen_entry_ids", []))
        new_entries = [e for e in entries if e.get("entryId") and e.get("entryId") not in seen_entry_ids]

        relayed = 0

        pending_events = dict(self.state.get("pending_events", {}))
        for incoming_id, event in pending_events.items():
            if self._attempt_event_publish(event):
                relayed += 1

        for entry in reversed(new_entries):
            event = self._extract_event_from_entry(entry)
            if not event:
                continue
            incoming_id = event["incoming_tweet"].get("id")
            if not incoming_id:
                continue
            if self._attempt_event_publish(event):
                relayed += 1

        self.state.setdefault("seen_entry_ids", []).extend(entry_ids)
        self.state["last_poll"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _save_state(self.state)
        return {
            "primed": 0,
            "relayed": relayed,
            "new_entries": len(new_entries),
            "pending": len(self.state.get("pending_events", {})),
        }

    def run(self):
        if not self._acquire_lock():
            return

        try:
            register_external_notification_source(
                TOOL_NAME,
                NOTIFICATION_SOURCE_ID,
                NOTIFICATION_SOURCE_LABEL,
            )
            migrated = _migrate_legacy_relay_targets()
            if migrated:
                self._log(
                    f"Migrated {migrated} legacy relay target"
                    f"{'s' if migrated != 1 else ''} to Exocortex subscriptions"
                )
            self._log(
                f"Notify service starting for @{self.self_handle} with "
                f"poll={self.poll_seconds}s threshold=<{self.max_parent_replies} replies"
            )
            while self.running:
                try:
                    stats = self.poll_once()
                    if stats["primed"]:
                        pass
                    elif stats["new_entries"] or stats["relayed"] or stats["pending"]:
                        self._log(
                            f"Poll complete: {stats['new_entries']} new notification entries, "
                            f"{stats['relayed']} published, {stats['pending']} pending"
                        )
                except Exception as e:
                    self._log(f"Poll error: {e}")
                    self._log(traceback.format_exc().rstrip())
                    self._sleep(30)
                    continue
                self._sleep(self.poll_seconds)
        finally:
            self._release_lock()
            self._log("Notify service stopped")


def _subscribe(argv, *, prog):
    p = argparse.ArgumentParser(
        prog=prog,
        description="Subscribe an Exocortex conversation to managed-tweet replies and quotes.",
    )
    p.add_argument("conv_id", help="Exo conversation ID")
    p.add_argument(
        "--delivery",
        choices=("wake", "inbox"),
        default="wake",
        help="wake starts/queues an agent turn; inbox stores without waking (default: wake)",
    )
    args = p.parse_args(argv)

    subscription = subscribe_external_notification(
        TOOL_NAME,
        NOTIFICATION_SOURCE_ID,
        args.conv_id,
        delivery=args.delivery,
        source_label=NOTIFICATION_SOURCE_LABEL,
    )
    delivery = subscription.get("delivery", args.delivery)
    print(f"  Subscribed: {args.conv_id} ({delivery})")


def subscribe(argv):
    _subscribe(argv, prog="twitter notify subscribe")


def add(argv):
    """Backward-compatible alias for subscribe."""
    _subscribe(argv, prog="twitter notify add")


def _unsubscribe(argv, *, prog):
    p = argparse.ArgumentParser(
        prog=prog,
        description="Unsubscribe an Exocortex conversation from Twitter notifications.",
    )
    p.add_argument("conv_id", help="Exo conversation ID")
    args = p.parse_args(argv)

    unsubscribe_external_notification(
        tool_name=TOOL_NAME,
        source_id=NOTIFICATION_SOURCE_ID,
        conv_id=args.conv_id,
    )
    print(f"  Unsubscribed: {args.conv_id}")


def unsubscribe(argv):
    _unsubscribe(argv, prog="twitter notify unsubscribe")


def remove(argv):
    """Backward-compatible alias for unsubscribe."""
    _unsubscribe(argv, prog="twitter notify remove")


def list_config(argv):
    p = argparse.ArgumentParser(prog="twitter notify list",
        description="Show Exocortex subscriptions and Twitter polling configuration.")
    p.parse_args(argv)

    cfg = _load_config()
    state = _load_state()
    pids = _find_notify_pids()
    subscriptions = list_external_notification_subscriptions(
        tool_name=TOOL_NAME,
        source_id=NOTIFICATION_SOURCE_ID,
    )

    print("  Subscriptions:")
    if subscriptions:
        for subscription in subscriptions:
            conv_id = subscription.get("convId", "?")
            delivery = subscription.get("delivery", "wake")
            enabled = "enabled" if subscription.get("enabled", True) else "disabled"
            subscription_id = subscription.get("id")
            suffix = f" [{subscription_id}]" if subscription_id else ""
            print(f"    • {conv_id} ({delivery}, {enabled}){suffix}")
    else:
        print("    (none)")

    legacy_targets = cfg.get("relay_targets", [])
    if legacy_targets:
        print(f"  Legacy targets awaiting daemon migration: {len(legacy_targets)}")

    print(f"  Poll interval: {cfg.get('poll_seconds', DEFAULT_POLL_SECONDS)}s")
    print(f"  Parent reply threshold: <{cfg.get('max_parent_replies', DEFAULT_MAX_PARENT_REPLIES)} replies")
    print(f"  Listener running: {'yes' if pids else 'no'}")
    if pids:
        print(f"  PIDs: {', '.join(str(pid) for pid in pids)}")
        if len(pids) > 1:
            print("  Warning: multiple notify listeners detected")
    print(f"  Pending publications: {len(state.get('pending_events', {}))}")
    managed_ids, _details = list_agent_tweets()
    print(f"  Agent-managed tweets: {len(managed_ids)}")
    if state.get("last_poll"):
        print(f"  Last poll: {state['last_poll']}")


def mark(argv):
    p = argparse.ArgumentParser(prog="twitter notify mark",
        description="Mark one of your tweets as agent-managed so replies/quotes are relayed.")
    p.add_argument("tweet", help="Tweet ID or URL")
    args = p.parse_args(argv)

    tweet_id = parse_tweet_ref(args.tweet)
    if not tweet_id:
        print("  Invalid tweet ID/URL", file=sys.stderr)
        sys.exit(1)
    record_agent_tweet(tweet_id, command="manual-mark")
    print(f"  Marked agent-managed tweet: {tweet_id}")


def unmark(argv):
    p = argparse.ArgumentParser(prog="twitter notify unmark",
        description="Remove a tweet from the agent-managed relay allowlist.")
    p.add_argument("tweet", help="Tweet ID or URL")
    args = p.parse_args(argv)

    tweet_id = parse_tweet_ref(args.tweet)
    if not tweet_id:
        print("  Invalid tweet ID/URL", file=sys.stderr)
        sys.exit(1)
    if unrecord_agent_tweet(tweet_id):
        print(f"  Unmarked agent-managed tweet: {tweet_id}")
    else:
        print(f"  Not marked: {tweet_id}")


def managed(argv):
    p = argparse.ArgumentParser(prog="twitter notify managed",
        description="List tweets whose replies/quotes can be relayed.")
    p.add_argument("-n", "--count", type=int, default=50, help="Number of tweets to show")
    args = p.parse_args(argv)

    ids, details = list_agent_tweets()
    if not ids:
        print("  No agent-managed tweets recorded")
        return
    for tweet_id in ids[-max(1, args.count):]:
        entry = details.get(tweet_id, {})
        command = entry.get("command", "?")
        text = (entry.get("text") or "").replace("\n", " ")[:100]
        suffix = f" — {text}" if text else ""
        print(f"  {tweet_id} [{command}]{suffix}")


def start(argv):
    p = argparse.ArgumentParser(prog="twitter notify start",
        description="Start the supervised Twitter notification poller.")
    p.parse_args(argv)

    existing_pids = _find_notify_pids()
    if existing_pids:
        print(
            f"  Found {len(existing_pids)} existing notify listener"
            f"{'s' if len(existing_pids) != 1 else ''}; restarting cleanly"
        )
        print(f"  Existing PIDs: {', '.join(str(pid) for pid in existing_pids)}")
        _stop_notify_pids(existing_pids)

    status = manage_external_tool_daemon("twitter", "start")
    print(f"  {status.get('message', 'Requested start for supervised Twitter daemon')}")


def stop(argv):
    p = argparse.ArgumentParser(prog="twitter notify stop",
        description="Stop the supervised Twitter notification poller.")
    p.parse_args(argv)

    status = manage_external_tool_daemon("twitter", "stop")
    print(f"  {status.get('message', 'Requested stop for supervised Twitter daemon')}")

    existing_pids = _find_notify_pids()
    if existing_pids:
        _stop_notify_pids(existing_pids)


def _run_daemon(argv):
    if len(argv) != 1:
        raise SystemExit("usage: python -m src.notify __run__ <log_file>")
    log_file = argv[0]
    service = TwitterNotifyService(log_file)
    service.run()


def help_command(argv):
    p = argparse.ArgumentParser(
        prog="twitter notify",
        description=(
            "Publish replies and quotes to agent-managed tweets through "
            "Exocortex's notification registry."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""commands:
  subscribe <conv_id>    Subscribe a conversation (--delivery wake|inbox)
  unsubscribe <conv_id>  Remove that conversation's subscription
  list                   Show subscriptions, poller status, and delivery state
  mark <tweet>           Mark a tweet as agent-managed
  unmark <tweet>         Stop managing a tweet
  managed                List agent-managed tweets
  start / stop           Manage the supervised notification poller

aliases:
  add = subscribe, remove = unsubscribe""",
    )
    p.parse_args(argv)
    p.print_help()


_COMMANDS = {
    "subscribe": subscribe,
    "unsubscribe": unsubscribe,
    "add": add,
    "remove": remove,
    "list": list_config,
    "mark": mark,
    "unmark": unmark,
    "managed": managed,
    "start": start,
    "stop": stop,
    "help": help_command,
}


def dispatch(cmd, argv):
    if not argv:
        list_config([])
        return

    subcmd = "help" if argv[0] in ("-h", "--help") else argv[0]
    fn = _COMMANDS.get(subcmd)
    if fn is None:
        print(f"  Unknown notify subcommand: {subcmd}")
        print(f"  Available: {', '.join(_COMMANDS.keys())}")
        sys.exit(1)
    fn(argv[1:])


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "__run__":
        _run_daemon(argv[1:])
    else:
        dispatch("notify", argv)


if __name__ == "__main__":
    main()
