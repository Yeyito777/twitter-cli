"""Notify subcommands — manage Twitter reply/quote relay."""

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
from src.auth import PROJECT_ROOT
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

    return {
        "relay_targets": list(cfg.get("relay_targets", [])),
        "poll_seconds": int(cfg.get("poll_seconds", DEFAULT_POLL_SECONDS)),
        "max_parent_replies": int(cfg.get("max_parent_replies", DEFAULT_MAX_PARENT_REPLIES)),
    }


def _save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n")


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
    return _load_config().get("relay_targets", [])


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
    def __init__(self, log_file, relay_targets=None):
        self.log_file = Path(log_file)
        self.relay_targets = list(relay_targets or [])
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

    def _relay_event(self, event, relay_targets=None):
        msg = self._format_event_message(event)
        incoming_id = event["incoming_tweet"]["id"]
        success = []
        failed = []
        targets = list(relay_targets or self.relay_targets)

        for conv_id in targets:
            try:
                result = subprocess.run(
                    ["exo", "send", msg, "-c", conv_id, "--timeout", "600"],
                    capture_output=True,
                    text=True,
                    timeout=660,
                )
                if result.returncode != 0:
                    failed.append(conv_id)
                    self._log(
                        f"Relay failed for tweet {incoming_id} to {conv_id}: "
                        f"{result.stderr.strip()[:300]}"
                    )
                else:
                    success.append(conv_id)
                    out = (result.stdout or "").strip()
                    if "queued" in out.lower():
                        self._log(f"Relayed tweet {incoming_id} to {conv_id}: auto-queued")
                    else:
                        self._log(f"Relayed tweet {incoming_id} to {conv_id}: delivered")
            except subprocess.TimeoutExpired:
                failed.append(conv_id)
                self._log(f"Relay timed out for tweet {incoming_id} to {conv_id}")
            except Exception as e:
                failed.append(conv_id)
                self._log(f"Relay error for tweet {incoming_id} to {conv_id}: {e}")

        return success, failed

    def _pending_targets_for(self, incoming_id):
        relay_map = self.state.setdefault("relayed_by_target", {})
        pending = []
        for conv_id in self.relay_targets:
            delivered = set(relay_map.get(conv_id, []))
            if incoming_id not in delivered:
                pending.append(conv_id)
        return pending

    def _mark_delivery_success(self, incoming_id, success_targets):
        relay_map = self.state.setdefault("relayed_by_target", {})
        for conv_id in success_targets:
            delivered = relay_map.setdefault(conv_id, [])
            delivered.append(incoming_id)

    def _mark_fully_relayed(self, incoming_id):
        self.state.setdefault("relayed_tweet_ids", []).append(incoming_id)
        self.state.setdefault("pending_events", {}).pop(incoming_id, None)

    def _attempt_event_relay(self, event):
        incoming_id = event["incoming_tweet"].get("id")
        if not incoming_id:
            return False

        pending_targets = self._pending_targets_for(incoming_id)
        if not pending_targets:
            self._mark_fully_relayed(incoming_id)
            return False

        success_targets, failed_targets = self._relay_event(event, pending_targets)
        if success_targets:
            self._mark_delivery_success(incoming_id, success_targets)

        if failed_targets:
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
            if self._attempt_event_relay(event):
                relayed += 1

        for entry in reversed(new_entries):
            event = self._extract_event_from_entry(entry)
            if not event:
                continue
            incoming_id = event["incoming_tweet"].get("id")
            if not incoming_id:
                continue
            if self._attempt_event_relay(event):
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

        self._log(
            f"Notify service starting for @{self.self_handle} with "
            f"poll={self.poll_seconds}s threshold=<{self.max_parent_replies} replies "
            f"targets={','.join(self.relay_targets) or '(none)'}"
        )
        try:
            while self.running:
                try:
                    stats = self.poll_once()
                    if stats["primed"]:
                        pass
                    elif stats["new_entries"] or stats["relayed"] or stats["pending"]:
                        self._log(
                            f"Poll complete: {stats['new_entries']} new notification entries, "
                            f"{stats['relayed']} fully relayed, {stats['pending']} pending"
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


def add(argv):
    p = argparse.ArgumentParser(prog="twitter notify add",
        description="Add an exo conversation as a relay target.")
    p.add_argument("conv_id", help="Exo conversation ID")
    args = p.parse_args(argv)

    cfg = _load_config()
    targets = cfg.setdefault("relay_targets", [])
    if args.conv_id in targets:
        print(f"  Already added: {args.conv_id}")
        return
    targets.append(args.conv_id)
    _save_config(cfg)
    print(f"  Added relay target: {args.conv_id}")
    print("  Restart notify listener for changes to take effect.")


def remove(argv):
    p = argparse.ArgumentParser(prog="twitter notify remove",
        description="Remove a relay target.")
    p.add_argument("conv_id", help="Exo conversation ID")
    args = p.parse_args(argv)

    cfg = _load_config()
    targets = cfg.get("relay_targets", [])
    if args.conv_id not in targets:
        print(f"  Not found: {args.conv_id}")
        return
    targets.remove(args.conv_id)
    _save_config(cfg)
    print(f"  Removed relay target: {args.conv_id}")
    print("  Restart notify listener for changes to take effect.")


def list_config(argv):
    p = argparse.ArgumentParser(prog="twitter notify list",
        description="Show notification relay configuration.")
    p.parse_args(argv)

    cfg = _load_config()
    state = _load_state()
    pids = _find_notify_pids()

    print("  Relay targets:")
    targets = cfg.get("relay_targets", [])
    if targets:
        for t in targets:
            print(f"    • {t}")
    else:
        print("    (none)")

    print(f"  Poll interval: {cfg.get('poll_seconds', DEFAULT_POLL_SECONDS)}s")
    print(f"  Parent reply threshold: <{cfg.get('max_parent_replies', DEFAULT_MAX_PARENT_REPLIES)} replies")
    print(f"  Listener running: {'yes' if pids else 'no'}")
    if pids:
        print(f"  PIDs: {', '.join(str(pid) for pid in pids)}")
        if len(pids) > 1:
            print("  Warning: multiple notify listeners detected")
    print(f"  Pending relays: {len(state.get('pending_events', {}))}")
    if state.get("last_poll"):
        print(f"  Last poll: {state['last_poll']}")


def start(argv):
    p = argparse.ArgumentParser(prog="twitter notify start",
        description="Start the Twitter notification relay listener.")
    p.parse_args(argv)

    cfg = _load_config()
    targets = cfg.get("relay_targets", [])
    if not targets:
        print("  No relay targets configured. Run: twitter notify add <conv_id>")
        return

    LISTENER_DIR.mkdir(parents=True, exist_ok=True)
    paths = _listener_paths()
    pid_file = paths["pid"]
    log_file = paths["log"]
    err_file = paths["err"]
    meta_file = paths["meta"]

    existing_pids = _find_notify_pids()
    if existing_pids:
        print(
            f"  Found {len(existing_pids)} existing notify listener"
            f"{'s' if len(existing_pids) != 1 else ''}; restarting cleanly"
        )
        print(f"  Existing PIDs: {', '.join(str(pid) for pid in existing_pids)}")
        _stop_notify_pids(existing_pids)

    pid_file.unlink(missing_ok=True)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT)

    proc = subprocess.Popen(
        [sys.executable, "-m", "src.notify", "__run__", str(log_file)],
        cwd=str(PROJECT_ROOT),
        env=env,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=open(err_file, "a"),
    )

    pid_file.write_text(str(proc.pid))
    meta = {
        "type": "notify",
        "relay_targets": targets,
        "poll_seconds": cfg.get("poll_seconds", DEFAULT_POLL_SECONDS),
        "max_parent_replies": cfg.get("max_parent_replies", DEFAULT_MAX_PARENT_REPLIES),
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    meta_file.write_text(json.dumps(meta, indent=2) + "\n")

    print(f"  Notify listener started (PID {proc.pid})")
    print(f"  Relaying to: {', '.join(targets)}")
    print(f"  Output: {log_file}")


def stop(argv):
    p = argparse.ArgumentParser(prog="twitter notify stop",
        description="Stop the Twitter notification relay listener.")
    p.parse_args(argv)

    paths = _listener_paths()
    pid_file = paths["pid"]
    meta_file = paths["meta"]

    pids = set(_find_notify_pids())
    if pid_file.exists():
        try:
            candidate = int(pid_file.read_text().strip())
            os.kill(candidate, 0)
            pids.add(candidate)
        except (ProcessLookupError, ValueError):
            pid_file.unlink(missing_ok=True)

    if not pids:
        print("  Notify listener not running")
        return

    _stop_notify_pids(pids)

    pid_file.unlink(missing_ok=True)
    meta_file.unlink(missing_ok=True)


def _run_daemon(argv):
    if not argv:
        raise SystemExit("usage: python -m src.notify __run__ <log_file> [conv_id ...]")
    log_file = argv[0]
    relay_targets = argv[1:] or _load_config().get("relay_targets", [])
    service = TwitterNotifyService(log_file, relay_targets=relay_targets)
    service.run()


_COMMANDS = {
    "add": add,
    "remove": remove,
    "list": list_config,
    "start": start,
    "stop": stop,
}


def dispatch(cmd, argv):
    if not argv:
        list_config([])
        return

    subcmd = argv[0]
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
