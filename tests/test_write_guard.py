import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src import accounts, api, fingerprint, transport, webclient, write_guard
from tests.helpers import SNAPSHOT, response

BIN = Path(__file__).resolve().parents[1] / "bin" / "twitter"
MAIN = {"user_id": "100", "screen_name": "main_user", "name": "Main", "owner": "user"}
TOKENS = {"auth_token": "a", "ct0": "c", "bearer": "b", "alias": "main"}


class _Account(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "accounts" / "main").mkdir(parents=True)
        (root / "accounts.json").write_text(json.dumps(
            {"version": 1, "default_account": "main", "accounts": {"main": MAIN}}))
        for item in (
            patch.object(accounts, "CONFIG_ROOT", root),
            patch.dict(os.environ, {accounts.ACCOUNT_ENV: "main", accounts.ACCOUNT_EXPLICIT_ENV: "1"}),
        ):
            item.start()
            self.addCleanup(item.stop)


class SelfCheckTests(_Account):
    def test_broken_web_client_keeps_every_write_path_paused_before_sending(self):
        send = Mock()
        with (
            patch.object(webclient, "load", side_effect=webclient.WebClientError("ondemand.s not found")),
            patch.object(api.transport, "request", send),
        ):
            for call in (
                lambda: api.graphql_post("q", "CreateTweet", {}, tokens=TOKENS),
                lambda: api.graphql_post("q", "FavoriteTweet", {}, tokens=TOKENS),
                lambda: api.rest_post("/1.1/dm/new2.json", {"text": "x"}, tokens=TOKENS),
                lambda: api.rest_post("/1.1/friendships/create.json", {"user_id": "1"}, form=True, tokens=TOKENS),
            ):
                with self.assertRaisesRegex(write_guard.WritePaused, "ondemand.s not found"):
                    call()
        send.assert_not_called()

    def test_missing_tls_impersonation_pauses_writes(self):
        with (
            patch.object(webclient, "load", return_value=SNAPSHOT),
            patch.object(transport, "impersonation_problem", return_value="curl_cffi is not installed"),
        ):
            problems, _ = write_guard.self_check(TOKENS, "CreateTweet")
        self.assertEqual(problems, ["curl_cffi is not installed"])

    def test_operation_missing_from_live_client_pauses_that_write(self):
        with patch.object(webclient, "load", return_value=SNAPSHOT):
            problems, _ = write_guard.self_check(TOKENS, "CreateNoteTweet")
        self.assertIn("CreateNoteTweet is not an operation of the live web client", problems)

    def test_healthy_self_check_lets_the_write_through_signed_and_live(self):
        send = Mock(return_value=response({"data": {"favorite_tweet": "Done"}}))
        with (
            patch.object(webclient, "load", return_value=SNAPSHOT),
            patch.object(api.transport, "request", send),
        ):
            api.graphql_post("stale-id", "FavoriteTweet", {"tweet_id": "1"}, tokens=TOKENS)
        method, url = send.call_args.args
        headers = dict(send.call_args.kwargs["headers"])
        body = json.loads(send.call_args.kwargs["data"])
        self.assertEqual((method, url), ("POST", "https://x.com/i/api/graphql/live-FavoriteTweet/FavoriteTweet"))
        self.assertEqual(body["queryId"], "live-FavoriteTweet")
        self.assertEqual(body["features"], {"f_on": True, "f_off": False})
        self.assertTrue(headers["x-client-transaction-id"])
        self.assertEqual(headers["user-agent"], fingerprint.USER_AGENT)
        self.assertEqual(headers["origin"], "https://x.com")
        self.assertFalse(send.call_args.kwargs["allow_fallback"])


class ReadTests(_Account):
    def test_reads_work_without_a_snapshot_and_are_not_gated(self):
        send = Mock(return_value=response({"data": {}}))
        with (
            patch.object(webclient, "load", side_effect=webclient.WebClientError("down")),
            patch.object(api.transport, "request", send),
        ):
            self.assertEqual(api.graphql_get("fallback-id", "TweetDetail", {}, tokens=TOKENS), {"data": {}})
        headers = dict(send.call_args.kwargs["headers"])
        self.assertNotIn("x-client-transaction-id", headers)
        self.assertNotIn("origin", headers)  # same-origin GET
        self.assertIn("/graphql/fallback-id/TweetDetail?", send.call_args.args[1])

    def test_reads_use_the_live_query_id_and_sign_when_available(self):
        send = Mock(return_value=response({"data": {}}))
        with (
            patch.object(webclient, "load", return_value=SNAPSHOT),
            patch.object(api.transport, "request", send),
        ):
            api.graphql_get("stale", "TweetDetail", {}, tokens=TOKENS)
        self.assertIn("/graphql/live-TweetDetail/TweetDetail?", send.call_args.args[1])
        self.assertIn("x-client-transaction-id", dict(send.call_args.kwargs["headers"]))

    def test_http_errors_carry_status_and_x_codes(self):
        error = response({"errors": [{"code": 326, "message": "locked"}]}, status=403)
        with (
            patch.object(webclient, "load", return_value=SNAPSHOT),
            patch.object(api.transport, "request", return_value=error),
            self.assertRaises(api.TwitterAPIError) as caught,
        ):
            api.graphql_get("q", "TweetDetail", {}, tokens=TOKENS)
        self.assertEqual((caught.exception.status, caught.exception.codes), (403, {326}))
        self.assertIsInstance(caught.exception, RuntimeError)


class CliTests(unittest.TestCase):
    def test_cli_write_without_account_exits_cleanly(self):
        with tempfile.TemporaryDirectory() as config:
            Path(config, "accounts.json").write_text(json.dumps(
                {"version": 1, "default_account": "main", "accounts": {"main": MAIN}}))
            result = subprocess.run(
                [str(BIN), "like", "123"], input="", text=True, capture_output=True,
                env={**os.environ, "TWITTER_CONFIG_DIR": config},
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("requires '-a/--account <alias>'", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
