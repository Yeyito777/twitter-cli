import json
import unittest
from unittest.mock import Mock, patch

from src import exocortex


class _ExistingSocketPath:
    def exists(self):
        return True

    def __str__(self):
        return "/tmp/test-exocortexd.sock"


class _ReplyingSocket:
    def __init__(self, response_type, response_fields):
        self.response_type = response_type
        self.response_fields = response_fields
        self.sent = None
        self.reply = None
        self.closed = False

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, path):
        self.path = path

    def sendall(self, data):
        self.sent = json.loads(data.decode("utf-8"))
        event = {
            "type": self.response_type,
            "reqId": self.sent["reqId"],
            **self.response_fields,
        }
        self.reply = (json.dumps(event) + "\n").encode("utf-8")

    def recv(self, size):
        if self.reply is None:
            return b""
        reply, self.reply = self.reply, None
        return reply

    def close(self):
        self.closed = True


class ExocortexRequestTests(unittest.TestCase):
    def test_generic_request_sends_newline_json_and_returns_matching_event(self):
        fake_socket = _ReplyingSocket("expected_result", {"value": 42})
        with (
            patch.object(exocortex, "_socket_path", return_value=_ExistingSocketPath()),
            patch.object(exocortex.socket, "socket", return_value=fake_socket),
        ):
            result = exocortex.exocortex_request(
                "example_command",
                "expected_result",
                toolName="twitter",
            )

        self.assertEqual(result["value"], 42)
        self.assertEqual(fake_socket.sent["type"], "example_command")
        self.assertEqual(fake_socket.sent["toolName"], "twitter")
        self.assertTrue(fake_socket.sent["reqId"])
        self.assertTrue(fake_socket.closed)

    def test_notification_helpers_build_core_wire_commands(self):
        request = Mock(side_effect=[
            {"source": {"id": "source"}},
            {"subscriptions": [{"id": "sub"}]},
            {"subscription": {"id": "new-sub"}},
            {"subscriptions": []},
            {"deliveries": []},
        ])
        with patch.object(exocortex, "exocortex_request", request):
            self.assertEqual(
                exocortex.register_external_notification_source(
                    "twitter", "source", "Source label"
                )["id"],
                "source",
            )
            self.assertEqual(
                exocortex.list_external_notification_subscriptions(
                    tool_name="twitter", source_id="source", conv_id="conv"
                )[0]["id"],
                "sub",
            )
            self.assertEqual(
                exocortex.subscribe_external_notification(
                    "twitter",
                    "source",
                    "conv",
                    delivery="inbox",
                    source_label="Source label",
                )["id"],
                "new-sub",
            )
            self.assertEqual(
                exocortex.unsubscribe_external_notification(
                    tool_name="twitter", source_id="source", conv_id="conv"
                ),
                [],
            )
            exocortex.publish_external_notification(
                "twitter", "source", "event-1", "hello", occurred_at=1234
            )

        commands = [call.args[:2] for call in request.call_args_list]
        self.assertEqual(commands, [
            ("register_external_notification_source", "external_notification_source"),
            ("list_external_notification_subscriptions", "external_notification_subscriptions"),
            ("subscribe_external_notification", "external_notification_subscription"),
            ("unsubscribe_external_notification", "external_notification_subscriptions"),
            ("publish_external_notification", "external_notification_publish_result"),
        ])
        publish_fields = request.call_args_list[-1].kwargs
        self.assertEqual(publish_fields["eventId"], "event-1")
        self.assertEqual(publish_fields["occurredAt"], 1234)

    def test_unsubscribe_requires_id_or_complete_source_tuple(self):
        with self.assertRaises(ValueError):
            exocortex.unsubscribe_external_notification(tool_name="twitter")


if __name__ == "__main__":
    unittest.main()
