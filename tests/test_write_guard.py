import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from src import accounts, api, write_guard

BIN = Path(__file__).resolve().parents[1] / "bin" / "twitter"


class WritePauseTests(unittest.TestCase):
    def test_every_write_path_refuses_before_network_while_paused(self):
        urlopen = Mock()
        with patch.object(api.urllib.request, "urlopen", urlopen), patch.object(api, "get_tokens") as tokens:
            for call in (
                lambda: api.graphql_post("q", "CreateTweet", {}),
                lambda: api.graphql_post("q", "FavoriteTweet", {}),
                lambda: api.rest_post("/1.1/dm/new2.json", {"text": "x"}),
                lambda: api.rest_post("/1.1/friendships/create.json", {"user_id": "1"}, form=True),
            ):
                with self.assertRaisesRegex(write_guard.WritePaused, "paused"):
                    call()
        urlopen.assert_not_called()
        tokens.assert_not_called()

    def test_reads_are_not_gated(self):
        response = Mock()
        response.read.return_value = b'{"data": {}}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        tokens = {"auth_token": "a", "ct0": "c", "bearer": "b"}
        with (
            patch.object(api.urllib.request, "urlopen", return_value=response) as urlopen,
            patch.object(api, "_generate_txid", return_value="tx"),
        ):
            self.assertEqual(api.graphql_get("q", "TweetDetail", {}, tokens=tokens), {"data": {}})
        self.assertEqual(urlopen.call_args.kwargs["timeout"], api.HTTP_TIMEOUT_SECONDS)

    def test_cli_write_exits_cleanly_with_the_pause_message(self):
        with tempfile.TemporaryDirectory() as config:
            result = subprocess.run(
                [str(BIN), "like", "123"], input="", text=True, capture_output=True,
                env={**os.environ, "TWITTER_CONFIG_DIR": config},
            )
        self.assertEqual(result.returncode, 3)
        self.assertIn("Twitter writes are paused", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_http_errors_carry_status_and_x_codes(self):
        body = json.dumps({"errors": [{"code": 326, "message": "locked"}]}).encode()
        error = urllib.error.HTTPError("https://x.com", 403, "Forbidden", {}, io.BytesIO(body))
        tokens = {"auth_token": "a", "ct0": "c", "bearer": "b"}
        with (
            patch.object(api.urllib.request, "urlopen", side_effect=error),
            patch.object(api, "_generate_txid", return_value="tx"),
            self.assertRaises(api.TwitterAPIError) as caught,
        ):
            api.graphql_get("q", "TweetDetail", {}, tokens=tokens)
        self.assertEqual((caught.exception.status, caught.exception.codes), (403, {326}))
        self.assertIsInstance(caught.exception, RuntimeError)
