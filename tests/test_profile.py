import contextlib
import io
import unittest
from unittest.mock import patch

from src import reading


def _profile_output(user_result):
    data = {"data": {"user": {"result": user_result}}}
    out = io.StringIO()
    with patch.object(reading, "graphql_get", return_value=data), contextlib.redirect_stdout(out):
        reading.profile(["@alice"])
    return out.getvalue()


class ProfileCountTests(unittest.TestCase):
    def test_reads_counts_from_current_user_fields(self):
        output = _profile_output({
            "core": {"name": "Alice", "screen_name": "alice"},
            "legacy": {},
            "relationship_counts": {"followers": 3070, "following": 1278},
            "tweet_counts": {"media_tweets": 724, "tweets": 4604},
        })
        self.assertIn("3.1K followers · 1.3K following · 4.6K tweets", output)

    def test_falls_back_to_legacy_counts(self):
        output = _profile_output({
            "core": {"name": "Alice", "screen_name": "alice"},
            "legacy": {"followers_count": 12, "friends_count": 34, "statuses_count": 56},
        })
        self.assertIn("12 followers · 34 following · 56 tweets", output)


if __name__ == "__main__":
    unittest.main()
