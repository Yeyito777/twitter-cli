import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src import accounts, api, engagement, posting, reading, write_guard

MAIN = {"user_id": "100", "screen_name": "main_user", "name": "Main", "owner": "user"}


def _response(payload):
    response = Mock()
    response.read.return_value = json.dumps(payload).encode()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    return response


class _Unpaused(unittest.TestCase):
    """Writes as if the fingerprint were healthy, against a throwaway account."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "accounts" / "main").mkdir(parents=True)
        (root / "accounts.json").write_text(json.dumps(
            {"version": 1, "default_account": "main", "accounts": {"main": MAIN}}))
        for item in (
            patch.object(accounts, "CONFIG_ROOT", root),
            patch.object(write_guard, "fingerprint_problem", return_value=None),
            patch.dict(os.environ, {accounts.ACCOUNT_ENV: "main", accounts.ACCOUNT_EXPLICIT_ENV: "1"}),
            patch.object(api, "get_tokens", return_value={"auth_token": "a", "ct0": "c", "bearer": "b"}),
            patch.object(api, "_generate_txid", return_value="tx"),
        ):
            item.start()
            self.addCleanup(item.stop)


class ExplicitAccountTests(_Unpaused):
    def test_write_without_explicit_account_is_refused_at_send_time(self):
        os.environ.pop(accounts.ACCOUNT_EXPLICIT_ENV)
        with patch.object(api.urllib.request, "urlopen") as urlopen:
            with self.assertRaisesRegex(write_guard.WritePaused, "explicit account"):
                api.graphql_post("q", "FavoriteTweet", {"tweet_id": "1"})
        urlopen.assert_not_called()


class FalseSuccessTests(_Unpaused):
    def test_graphql_errors_are_failures_not_success(self):
        stdout = io.StringIO()
        error = {"errors": [{"code": 139, "message": "You have already favorited this post."}]}
        with (
            patch.object(api.urllib.request, "urlopen", return_value=_response(error)),
            contextlib.redirect_stdout(stdout),
            self.assertRaisesRegex(api.TwitterAPIError, "already favorited"),
        ):
            engagement.like(["123"])
        self.assertNotIn("Liked", stdout.getvalue())

    def test_failed_delete_keeps_the_managed_tweet_record(self):
        error = {"errors": [{"code": 144, "message": "No status found with that ID."}]}
        with (
            patch.object(api.urllib.request, "urlopen", return_value=_response(error)),
            patch.object(posting, "unrecord_agent_tweet") as unrecord,
            self.assertRaises(api.TwitterAPIError),
        ):
            posting.delete(["123"])
        unrecord.assert_not_called()

    def test_automated_answer_pauses_all_further_writes(self):
        flagged = {"errors": [{"code": 226, "message": "This request looks like it might be automated."}]}
        with patch.object(api.urllib.request, "urlopen", return_value=_response(flagged)) as urlopen:
            with self.assertRaises(api.TwitterAPIError):
                api.graphql_post("q", "CreateTweet", {"tweet_text": "x"})
            with self.assertRaisesRegex(write_guard.WritePaused, "226"):
                api.graphql_post("q", "FavoriteTweet", {"tweet_id": "1"})
        self.assertEqual(urlopen.call_count, 1)
        self.assertIsNone(write_guard.active_lockout()["until"])
        self.assertTrue(write_guard.clear_lockout("main"))
        self.assertIsNone(write_guard.active_lockout())

    def test_daily_limit_pauses_for_a_day_then_expires(self):
        marker = write_guard.record_write_error([344], "daily limit", "CreateTweet")
        self.assertAlmostEqual(marker["until"], time.time() + 86400, delta=5)
        with patch.object(write_guard.time, "time", return_value=marker["until"] + 1):
            self.assertIsNone(write_guard.active_lockout())

    def test_http_error_codes_also_pause(self):
        import urllib.error
        body = json.dumps({"errors": [{"code": 326, "message": "locked"}]}).encode()
        error = urllib.error.HTTPError("https://x.com", 403, "Forbidden", {}, io.BytesIO(body))
        with patch.object(api.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(api.TwitterAPIError):
                api.rest_post("/1.1/friendships/create.json", {"user_id": "1"}, form=True)
        self.assertEqual(write_guard.active_lockout()["codes"], [326])


class DmTargetTests(_Unpaused):
    INBOX = {"inbox_initial_state": {"conversations": {
        "g-1": {"type": "GROUP_DM", "participants": [{"user_id": "100"}, {"user_id": "42"}, {"user_id": "9"}]},
        "42-100": {"type": "ONE_TO_ONE", "participants": [{"user_id": "100"}, {"user_id": "42"}]},
    }}}

    def send(self, inbox):
        posts = []
        stdout = io.StringIO()
        with (
            patch.object(reading, "resolve_user_id", return_value=("42", {})),
            patch.object(reading, "rest_get", return_value=inbox),
            patch.object(api, "rest_post", side_effect=lambda ep, body=None, **k: posts.append(body) or {}),
            contextlib.redirect_stdout(stdout),
        ):
            reading._dm_send("@alice", "hi")
        return posts[0], stdout.getvalue()

    def test_dm_to_user_uses_the_1to1_not_a_group_containing_them(self):
        payload, out = self.send(self.INBOX)
        self.assertEqual(payload["conversation_id"], "42-100")
        self.assertIn("request_id", payload)
        self.assertIn("DM sent to @alice (1:1 conversation 42-100)", out)

    def test_dm_with_only_a_shared_group_starts_a_new_1to1(self):
        inbox = {"inbox_initial_state": {"conversations": {
            "g-1": self.INBOX["inbox_initial_state"]["conversations"]["g-1"]}}}
        payload, out = self.send(inbox)
        self.assertNotIn("conversation_id", payload)
        self.assertEqual(payload["recipient_ids"], ["42"])
        self.assertIn("new 1:1 conversation", out)
