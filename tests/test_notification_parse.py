import unittest

from src.parse import parse_notification


def item(timestamp):
    return {
        "__typename": "TimelineNotification",
        "rich_message": {"text": "Makr liked your reply"},
        "notification_url": {"url": "https://twitter.com/im_yeyito/status/1"},
        "notification_icon": "heart_icon",
        "timestamp_ms": timestamp,
        "id": "n1",
    }


class NotificationTimestampTests(unittest.TestCase):
    def test_reads_iso_timestamp(self):
        self.assertEqual(parse_notification(item("2026-11-07T15:10:23.587Z"))["created_at"], "2026-11-07 15:10")

    def test_reads_epoch_milliseconds(self):
        self.assertEqual(parse_notification(item("1794064223587"))["created_at"], "2026-11-07 15:10")

    def test_unreadable_timestamp_is_blank(self):
        self.assertEqual(parse_notification(item("soon"))["created_at"], "")


if __name__ == "__main__":
    unittest.main()
