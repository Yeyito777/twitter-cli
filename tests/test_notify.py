import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src import notify


def _tweet(tweet_id, handle):
    return {
        "id": tweet_id,
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
    service.state = state or {
        "relayed_tweet_ids": [],
        "relayed_by_target": {},
        "pending_events": {},
    }
    service._log = Mock()
    return service


class NotifyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name)
        self.patches = [
            patch.object(notify, "CONFIG_DIR", self.config_dir),
            patch.object(notify, "CONFIG_FILE", self.config_dir / "notify.json"),
            patch.object(notify, "STATE_FILE", self.config_dir / "notify-state.json"),
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
        service = _bare_service()
        service.running = False
        service.self_handle = "agent"
        service.poll_seconds = 300
        service.max_parent_replies = 10
        service._acquire_lock = Mock(return_value=True)
        service._release_lock = Mock()
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
