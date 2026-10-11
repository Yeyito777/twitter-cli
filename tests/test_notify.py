import contextlib
import io
import json
import os
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src import accounts, notify


_config = tempfile.TemporaryDirectory()


def setUpModule():
    # Never read or write the real accounts, credentials, or poll state.
    for item in (
        patch.object(accounts, "CONFIG_ROOT", Path(_config.name)),
        patch.dict(os.environ, {}),
    ):
        item.start()
        unittest.addModuleCleanup(item.stop)
    os.environ.pop(accounts.ACCOUNT_ENV, None)
    os.environ.pop(accounts.ACCOUNT_EXPLICIT_ENV, None)


def _tweet(tweet_id, handle, author_id=""):
    return {
        "id": tweet_id,
        "author_id": author_id,
        "handle": handle,
        "name": handle.title(),
        "created_at": "2026-07-15 12:00",
        "text": f"tweet {tweet_id}",
        "is_retweet": False,
        "is_quote": False,
        "is_reply": False,
        "in_reply_to": None,
        "media": [],
        "likes": 0,
        "retweets": 0,
        "replies": 0,
        "views": 0,
    }


def _event(tweet_id="incoming-1"):
    return {
        "event_type": "direct_reply",
        "priority": "high",
        "reason": "test",
        "source": "tweet_item",
        "incoming_tweet": _tweet(tweet_id, "other"),
        "parent_tweet": _tweet("parent-1", "self"),
        "thread_items": [],
    }


def _bare_service(state=None):
    service = object.__new__(notify.TwitterNotifyService)
    service.alias = None
    service.meta = {"alias": None}
    service.source = {"id": notify.NOTIFICATION_SOURCE_ID, "label": notify.NOTIFICATION_SOURCE_LABEL}
    service.state_file = Path(_config.name) / "notify-state.json"
    service.tokens = {"auth_token": "token", "ct0": "csrf", "bearer": "bearer"}
    service.state = state or {
        "relayed_tweet_ids": [],
        "relayed_by_target": {},
        "pending_events": {},
    }
    service.retry_delay = 30
    service.last_error = None
    service._log = Mock()
    return service


class NotifyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name)
        self.patches = [
            patch.object(notify, "CONFIG_DIR", self.config_dir),
            patch.object(notify, "CONFIG_FILE", self.config_dir / "notify.json"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp_dir.cleanup()

    def _write_config(self, data):
        notify.CONFIG_FILE.write_text(json.dumps(data))

    def test_migration_imports_missing_targets_and_preserves_other_config(self):
        self._write_config({
            "relay_targets": ["conv-a", "conv-b", "conv-b"],
            "poll_seconds": 91,
            "max_parent_replies": 4,
            "future_filter": {"language": "en"},
        })
        subscribe = Mock(return_value={"id": "sub-b"})
        with (
            patch.object(
                notify,
                "list_external_notification_subscriptions",
                return_value=[{"convId": "conv-a", "delivery": "inbox"}],
            ),
            patch.object(notify, "subscribe_external_notification", subscribe),
        ):
            migrated = notify._migrate_legacy_relay_targets()

        self.assertEqual(migrated, 2)
        subscribe.assert_called_once_with(
            notify.TOOL_NAME,
            notify.NOTIFICATION_SOURCE_ID,
            "conv-b",
            delivery="wake",
            source_label=notify.NOTIFICATION_SOURCE_LABEL,
        )
        saved = json.loads(notify.CONFIG_FILE.read_text())
        self.assertNotIn("relay_targets", saved)
        self.assertEqual(saved["poll_seconds"], 91)
        self.assertEqual(saved["max_parent_replies"], 4)
        self.assertEqual(saved["future_filter"], {"language": "en"})

    def test_migration_keeps_legacy_key_when_any_import_fails(self):
        original = {
            "relay_targets": ["conv-a", "conv-b"],
            "poll_seconds": 300,
        }
        self._write_config(original)
        with (
            patch.object(notify, "list_external_notification_subscriptions", return_value=[]),
            patch.object(
                notify,
                "subscribe_external_notification",
                side_effect=[{"id": "sub-a"}, RuntimeError("core unavailable")],
            ),
            self.assertRaisesRegex(RuntimeError, "core unavailable"),
        ):
            notify._migrate_legacy_relay_targets()

        self.assertEqual(json.loads(notify.CONFIG_FILE.read_text()), original)

    def test_no_legacy_key_does_not_touch_core_or_config(self):
        original = {"poll_seconds": 45, "custom": True}
        self._write_config(original)
        list_subscriptions = Mock()
        with patch.object(
            notify, "list_external_notification_subscriptions", list_subscriptions
        ):
            self.assertEqual(notify._migrate_legacy_relay_targets(), 0)
        list_subscriptions.assert_not_called()
        self.assertEqual(json.loads(notify.CONFIG_FILE.read_text()), original)


class NotifyPublishTests(unittest.TestCase):
    def test_event_is_formatted_and_published_once_for_all_subscriptions(self):
        service = _bare_service()
        event = _event()
        publish = Mock(return_value={
            "eventId": "incoming-1",
            "deliveries": [
                {"convId": "conv-a", "status": "started"},
                {"convId": "conv-b", "status": "inbox"},
            ],
        })
        with patch.object(notify, "publish_external_notification", publish):
            self.assertTrue(service._attempt_event_publish(event))

        publish.assert_called_once()
        args = publish.call_args.args
        self.assertEqual(args[:3], (
            notify.TOOL_NAME,
            notify.NOTIFICATION_SOURCE_ID,
            "incoming-1",
        ))
        self.assertIn("[Twitter Reply Notification]", args[3])
        self.assertIn("incoming-1", service.state["relayed_tweet_ids"])
        self.assertNotIn("incoming-1", service.state["pending_events"])

    def test_failed_core_delivery_retains_event_for_stable_id_retry(self):
        service = _bare_service()
        event = _event()
        with patch.object(
            notify,
            "publish_external_notification",
            return_value={
                "eventId": "incoming-1",
                "deliveries": [{"convId": "conv-a", "status": "failed"}],
            },
        ):
            self.assertFalse(service._attempt_event_publish(event))

        self.assertEqual(service.state["pending_events"]["incoming-1"], event)
        self.assertNotIn("incoming-1", service.state["relayed_tweet_ids"])

    def test_existing_legacy_delivery_state_prevents_republish(self):
        service = _bare_service({
            "relayed_tweet_ids": ["incoming-1"],
            "relayed_by_target": {"old-conv": ["incoming-1"]},
            "pending_events": {"incoming-1": _event()},
        })
        publish = Mock()
        with patch.object(notify, "publish_external_notification", publish):
            self.assertFalse(service._attempt_event_publish(_event()))

        publish.assert_not_called()
        self.assertNotIn("incoming-1", service.state["pending_events"])
        self.assertEqual(
            service.state["relayed_by_target"], {"old-conv": ["incoming-1"]}
        )


class NotifyIdentityTests(unittest.TestCase):
    def _response(self, viewer_id="123", instructions=None):
        return {
            "data": {"viewer_v2": {"user_results": {"result": {
                "__typename": "User",
                "rest_id": viewer_id,
                "notification_timeline": {"timeline": {
                    "instructions": instructions if instructions is not None else [],
                }},
            }}}},
        }

    def test_constructor_does_not_make_a_network_request(self):
        with (
            patch.object(notify, "_load_state", return_value={}),
            patch.object(notify, "graphql_get") as graphql,
        ):
            service = notify.TwitterNotifyService(None, Mock(), poll_seconds=300, max_parent_replies=10)
        self.assertIsNone(service.self_id)
        graphql.assert_not_called()

    def test_notification_response_supplies_identity_before_entries_are_processed(self):
        service = _bare_service()
        service.self_id = None
        service.fetch_count = 50
        entries = [{"entryId": "entry-1"}]
        response = self._response(instructions=[
            {"type": "TimelineAddEntries", "entries": entries},
        ])
        with patch.object(notify, "graphql_get", return_value=response) as graphql:
            self.assertEqual(service._fetch_notification_entries(), entries)
        self.assertEqual(service.self_id, "123")
        graphql.assert_called_once_with(
            notify.Q["NotificationsTimeline"], "NotificationsTimeline",
            {"timeline_type": "All", "count": 50}, tokens=service.tokens,
        )

    def test_invalid_viewer_or_missing_timeline_does_not_advance_state(self):
        service = _bare_service({"initialized": False})
        service.self_id = None
        service.fetch_count = 50
        missing_timeline = self._response()
        del missing_timeline["data"]["viewer_v2"]["user_results"]["result"]["notification_timeline"]
        responses = [
            {}, self._response(viewer_id=""), self._response(viewer_id="not-an-id"),
            missing_timeline,
        ]
        for response in responses:
            with (
                self.subTest(response=response),
                patch.object(notify, "get_tokens", return_value=service.tokens),
                patch.object(notify, "graphql_get", return_value=response),
                patch.object(notify, "_save_state") as save,
                self.assertRaisesRegex(RuntimeError, "NotificationsTimeline"),
            ):
                service.poll_once()
            save.assert_not_called()
            self.assertFalse(service.state["initialized"])
            self.assertIsNone(service.self_id)

    def test_identity_is_refreshed_on_each_poll(self):
        service = _bare_service()
        service.self_id = "old-account"
        service.fetch_count = 50
        with patch.object(notify, "graphql_get", return_value=self._response("456")):
            service._fetch_notification_entries()
        self.assertEqual(service.self_id, "456")

    def test_own_tweet_detection_uses_ids_and_fails_closed_without_identity(self):
        service = _bare_service()
        service.self_id = "123"
        self.assertTrue(service._is_own_tweet(_tweet("1", "renamed", "123")))
        self.assertFalse(service._is_own_tweet(_tweet("1", "same-handle", "456")))
        self.assertFalse(service._is_own_tweet(_tweet("1", "unknown")))
        service.self_id = None
        self.assertFalse(service._is_own_tweet(_tweet("1", "unknown")))

    def test_reply_to_managed_tweet_is_selected_by_parent_author_id(self):
        service = _bare_service()
        service.self_id = "123"
        incoming = _tweet("incoming", "other", "456")
        incoming.update(is_reply=True, in_reply_to_id="parent")
        parent = _tweet("parent", "renamed-owner", "123")
        with (
            patch.object(service, "_fetch_tweet", return_value=parent),
            patch.object(service, "_fetch_thread_items", return_value=[]),
            patch.object(notify, "is_agent_tweet", return_value=True),
        ):
            service.max_parent_replies = 10
            self.assertEqual(service._classify_tweet(incoming)["event_type"], "direct_reply")
        self.assertIsNone(service._classify_tweet(_tweet("own", "new-handle", "123")))

    def test_quote_to_managed_tweet_is_selected_by_quoted_author_id(self):
        service = _bare_service()
        service.self_id = "123"
        incoming = _tweet("incoming", "other", "456")
        incoming.update(is_quote=True, quoted=_tweet("parent", "renamed-owner", "123"))
        with patch.object(notify, "is_agent_tweet", return_value=True):
            self.assertEqual(service._classify_tweet(incoming)["event_type"], "quote_tweet")
        incoming["quoted"]["author_id"] = "789"
        self.assertIsNone(service._classify_tweet(incoming))


class NotifyRetryTests(unittest.TestCase):
    def test_poll_errors_back_off_and_recovery_resets_delay(self):
        service = _bare_service()
        service.poll_seconds = 300
        success = {"primed": 0, "new_entries": 0, "relayed": 0, "pending": 0}
        service.poll_once = Mock(side_effect=[
            *[RuntimeError("HTTP 404") for _ in range(6)],
            success, RuntimeError("HTTP 404"),
        ])

        with patch.object(notify.traceback, "format_exc", return_value="trace") as trace:
            delays = [service.poll() for _ in range(8)]

        self.assertEqual(delays, [30, 60, 120, 240, 300, 300, 300, 30])
        self.assertEqual(trace.call_count, 2)
        service._log.assert_any_call("Notification polling recovered")

    def test_daemon_polls_each_account_on_its_own_schedule(self):
        daemon = object.__new__(notify.NotifyDaemon)
        daemon.running = True
        daemon.poll_seconds = 300
        daemon.max_parent_replies = 10
        daemon._acquire_lock = Mock(return_value=True)
        daemon._release_lock = Mock()
        daemon._log = Mock()
        healthy, failing = _bare_service(), _bare_service()
        healthy.poll = Mock(side_effect=lambda: setattr(healthy, "next_poll_at", time.monotonic() + 300))
        failing.poll = Mock(side_effect=lambda: setattr(failing, "next_poll_at", time.monotonic() + 30))
        healthy.next_poll_at = failing.next_poll_at = 0.0
        daemon.services = {"main": healthy, "alt": failing}
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            daemon.running = False

        daemon._sleep = sleep
        with (
            patch.object(daemon, "_sync_services"),
            patch.object(notify, "_migrate_legacy_relay_targets", return_value=0),
        ):
            daemon.run()

        healthy.poll.assert_called_once()
        failing.poll.assert_called_once()
        self.assertEqual(len(sleeps), 1)
        self.assertLessEqual(sleeps[0], 30)
        self.assertGreater(sleeps[0], 29)


class NotifyShutdownTests(unittest.TestCase):
    def test_sigterm_ends_a_poll_sleep_immediately(self):
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        self.addCleanup(lambda: [signal.signal(sig, handler) for sig, handler in previous.items()])
        with patch.object(notify, "_load_config", return_value={}):
            service = notify.NotifyDaemon("/unused/notify.log")
        service._log = Mock()

        child = os.fork()
        if child == 0:
            time.sleep(0.2)
            os.kill(os.getppid(), signal.SIGTERM)
            os._exit(0)
        started = time.monotonic()
        service._sleep(30)
        elapsed = time.monotonic() - started
        os.waitpid(child, 0)

        self.assertFalse(service.running)
        self.assertLess(elapsed, 0.6)


class NotifyCommandTests(unittest.TestCase):
    def test_add_and_remove_remain_registry_aliases(self):
        subscribe = Mock(return_value={"delivery": "inbox"})
        unsubscribe = Mock(return_value=[])
        output = io.StringIO()
        with (
            patch.object(notify, "subscribe_external_notification", subscribe),
            patch.object(notify, "unsubscribe_external_notification", unsubscribe),
            contextlib.redirect_stdout(output),
        ):
            notify.add(["conv-a", "--delivery", "inbox"])
            notify.remove(["conv-a"])

        subscribe.assert_called_once_with(
            notify.TOOL_NAME,
            notify.NOTIFICATION_SOURCE_ID,
            "conv-a",
            delivery="inbox",
            source_label=notify.NOTIFICATION_SOURCE_LABEL,
        )
        unsubscribe.assert_called_once_with(
            tool_name=notify.TOOL_NAME,
            source_id=notify.NOTIFICATION_SOURCE_ID,
            conv_id="conv-a",
        )
        self.assertIn("Subscribed", output.getvalue())
        self.assertIn("Unsubscribed", output.getvalue())

    def test_daemon_run_registers_source_before_migration(self):
        service = object.__new__(notify.NotifyDaemon)
        service.running = False
        service.poll_seconds = 300
        service.max_parent_replies = 10
        service.services = {}
        service._acquire_lock = Mock(return_value=True)
        service._release_lock = Mock()
        service._log = Mock()
        calls = []

        with (
            patch.object(
                notify,
                "register_external_notification_source",
                side_effect=lambda *args, **kwargs: calls.append("register"),
            ) as register,
            patch.object(
                notify,
                "_migrate_legacy_relay_targets",
                side_effect=lambda: calls.append("migrate") or 1,
            ),
            patch.object(notify, "notify_aliases", return_value=[None]),
        ):
            service.run()

        self.assertEqual(calls, ["register", "migrate"])
        register.assert_called_once_with(
            notify.TOOL_NAME,
            notify.NOTIFICATION_SOURCE_ID,
            notify.NOTIFICATION_SOURCE_LABEL,
        )
        service._release_lock.assert_called_once()

    def test_notify_help_describes_registry_commands_and_aliases(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            notify.dispatch("notify", ["--help"])
        text = output.getvalue()
        self.assertIn("subscribe <conv_id>", text)
        self.assertIn("unsubscribe <conv_id>", text)
        self.assertIn("add = subscribe", text)


if __name__ == "__main__":
    unittest.main()
