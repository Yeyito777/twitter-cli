"""X's x-web client (Vite + Relay, from Nov 18 2026): snapshot and writes."""

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import bs4
from x_client_transaction import ClientTransaction

from src import accounts, api, engagement, posting, transport, webclient, write_guard

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "xweb"
HOME = (FIXTURES / "home.html").read_text()
TOKENS = {"auth_token": "a", "ct0": "c", "bearer": "b", "alias": "main"}
MAIN = {"user_id": "100", "screen_name": "main_user", "name": "Main", "owner": "user"}


def fake_x(page=HOME, answers=None, preflight_status=200):
    """transport.request standing in for x.com, abs.twimg.com and api.x.com."""
    answers = answers or {}

    def request(method, url, *, headers, data=None, **_kwargs):
        if url == webclient.HOME_URL:
            return transport.Response(200, page.encode(), url)
        if url.startswith(webclient.XWEB_BASE):
            name = url.rsplit("/", 1)[1]
            path = FIXTURES / name
            if path.exists() and (url == webclient.XWEB_BASE + name) == name.startswith("entry-client-"):
                return transport.Response(200, path.read_bytes(), url)
            return transport.Response(404, b"", url)
        if method == "OPTIONS":
            return transport.Response(preflight_status, b"", url)
        for prefix, answer in answers.items():
            if url.startswith(prefix):
                return transport.Response(200, json.dumps(answer).encode(), url)
        return transport.Response(404, b"{}", url)

    return Mock(side_effect=request)


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
            patch.dict(os.environ, {"XDG_CACHE_HOME": str(root / "cache"), accounts.ACCOUNT_ENV: "main",
                                    accounts.ACCOUNT_EXPLICIT_ENV: "1"}),
            patch.object(api, "get_tokens", return_value=TOKENS),
        ):
            item.start()
            self.addCleanup(item.stop)
        webclient._memory.clear()
        self.addCleanup(webclient._memory.clear)

    def snapshot(self):
        with patch.object(transport, "request", fake_x()) as x:
            snapshot = webclient.fetch(TOKENS)
        return snapshot, [c.args[1].rsplit("/", 1)[1] for c in x.call_args_list[1:]]


class SnapshotTests(_Account):
    def test_snapshot_from_the_x_web_page(self):
        snapshot, scripts = self.snapshot()
        self.assertEqual(snapshot["layout"], "x-web")
        self.assertEqual(snapshot["client"], "entry-client-Te5tEntr.js")
        self.assertEqual(snapshot["txid_module"], "Txid0001.js")
        self.assertEqual(snapshot["operations"]["FavoriteTweet"]["queryId"], "fav-id")
        self.assertEqual(snapshot["rest_writes"], sorted(webclient.XWEB_REST_WRITES))
        self.assertEqual(snapshot["switches"]["rweb_client_transaction_id_enabled"], True)
        for name in [*webclient.XWEB_MUTATIONS, *webclient.XWEB_REST_WRITES]:
            self.assertIsNone(webclient.write_problem(snapshot, name), name)
        # Only what was needed: the unrelated lazy chunks are never fetched.
        self.assertNotIn("Unused01.js", scripts)
        self.assertNotIn("Unused02.js", scripts)

    def test_transaction_id_uses_the_indices_of_the_page_loaded_module(self):
        snapshot, _ = self.snapshot()
        module = (FIXTURES / "Txid0001.js").read_text()
        reference = ClientTransaction(home_page_response=bs4.BeautifulSoup(HOME, "html.parser"),
                                      ondemand_file_response=module)
        self.assertEqual((reference.row_index, reference.key_bytes_indices), (19, [24, 16, 19]))
        now = 1_795_000_000
        path = "/graphql/fav-id/FavoriteTweet"
        with patch("x_client_transaction.transaction.random.randint", return_value=9):
            expected = reference.generate_transaction_id("POST", path, time_now=now - webclient.TXID_EPOCH)
        self.assertEqual(webclient.transaction_id(snapshot, "POST", path, now=now, random_byte=9), expected)

    def test_no_transaction_module_is_a_failure(self):
        page = HOME.replace('["Txid0001"]', '["Route002"]')
        with patch.object(transport, "request", fake_x(page=page)):
            with self.assertRaisesRegex(webclient.WebClientError, "transaction-ID module"):
                webclient.fetch(TOKENS)

    def test_page_with_neither_client_is_a_failure(self):
        page = HOME.replace("entry-client-Te5tEntr.js", "other.js")
        with patch.object(transport, "request", fake_x(page=page)):
            with self.assertRaisesRegex(webclient.WebClientError, "neither"):
                webclient.fetch(TOKENS)

    def test_missing_write_operation_pauses_only_that_write(self):
        snapshot, _ = self.snapshot()
        del snapshot["operations"]["BookmarkAdd"]
        snapshot["rest_writes"].remove("/1.1/blocks/create.json")
        self.assertIn("BookmarkAdd", webclient.write_problem(snapshot, "CreateBookmark"))
        self.assertIn("not found", webclient.write_problem(snapshot, "/1.1/blocks/create.json"))
        self.assertIsNone(webclient.write_problem(snapshot, "FavoriteTweet"))

    def test_reads_keep_the_legacy_query_ids(self):
        snapshot, _ = self.snapshot()
        snapshot["operations"].update(webclient.parse_relay_operations((FIXTURES / "Router01.js").read_text()))
        self.assertEqual(snapshot["operations"]["UserByScreenName"]["queryId"], "user-q-id")
        self.assertIsNone(webclient.operation(snapshot, "UserByScreenName"))
        send = Mock(return_value=transport.Response(200, b'{"data":{}}', "https://x.com"))
        with patch.object(webclient, "load", return_value=snapshot), patch.object(api.transport, "request", send):
            api.graphql_get("legacy-id", "UserByScreenName", {"screen_name": "x"})
        self.assertTrue(send.call_args.args[1].startswith("https://x.com/i/api/graphql/legacy-id/UserByScreenName?"))


class WriteTests(_Account):
    def setUp(self):
        super().setUp()
        self.live, _ = self.snapshot()
        loader = patch.object(webclient, "load", return_value=self.live)
        loader.start()
        self.addCleanup(loader.stop)

    def test_like_is_sent_as_the_x_web_client_sends_it(self):
        x = fake_x(answers={"https://api.x.com/graphql/fav-id/": {"data": {"favorite_tweet": "Done"}}})
        out = io.StringIO()
        with patch.object(api.transport, "request", x), contextlib.redirect_stdout(out):
            engagement.like(["123"])
        self.assertIn("Liked.", out.getvalue())
        (pre_method, pre_url), (method, url) = [c.args for c in x.call_args_list]
        self.assertEqual((pre_method, pre_url), ("OPTIONS", "https://api.x.com/graphql/fav-id/FavoriteTweet"))
        self.assertEqual((method, url), ("POST", "https://api.x.com/graphql/fav-id/FavoriteTweet"))
        body = json.loads(x.call_args.kwargs["data"])
        self.assertEqual(body, {"queryId": "fav-id", "variables": {"tweetId": "123"}})
        headers = dict(x.call_args.kwargs["headers"])
        self.assertEqual(headers["sec-fetch-site"], "same-site")
        self.assertEqual(headers["origin"], "https://x.com")
        self.assertEqual(headers["referer"], "https://x.com/")
        self.assertEqual(headers["cookie"], "auth_token=a; ct0=c")
        self.assertTrue(headers["x-client-transaction-id"])
        preflight = dict(x.call_args_list[0].kwargs["headers"])
        self.assertNotIn("cookie", preflight)
        self.assertEqual(preflight["access-control-request-method"], "POST")
        self.assertEqual(preflight["access-control-request-headers"],
                         "authorization,content-type,x-client-transaction-id,x-csrf-token,"
                         "x-twitter-active-user,x-twitter-auth-type,x-twitter-client-language")
        self.assertFalse(x.call_args.kwargs["allow_fallback"])

    def test_preflight_is_cached_like_chrome_does(self):
        x = fake_x(answers={"https://api.x.com/graphql/": {"data": {"favorite_tweet": "Done", "unfavorite_tweet": "Done"}}})
        with patch.object(api.transport, "request", x), contextlib.redirect_stdout(io.StringIO()):
            engagement.like(["1"])
            engagement.like(["2"])
            with patch.object(api.time, "time", return_value=time.time() + api.PREFLIGHT_TTL + 1):
                engagement.like(["3"])
        self.assertEqual([c.args[0] for c in x.call_args_list], ["OPTIONS", "POST", "POST", "OPTIONS", "POST"])

    def test_refused_preflight_sends_nothing(self):
        x = fake_x(preflight_status=403)
        with patch.object(api.transport, "request", x), self.assertRaisesRegex(api.TwitterAPIError, "nothing was sent"):
            engagement.like(["1"])
        self.assertEqual([c.args[0] for c in x.call_args_list], ["OPTIONS"])

    def test_answer_without_the_mutation_field_is_not_a_success(self):
        x = fake_x(answers={"https://api.x.com/graphql/": {"data": {"favorite_tweet": None}}})
        out = io.StringIO()
        with (
            patch.object(api.transport, "request", x), contextlib.redirect_stdout(out),
            self.assertRaisesRegex(api.TwitterAPIError, "Outcome unclear"),
        ):
            engagement.like(["1"])
        self.assertNotIn("Liked", out.getvalue())

    def test_unretweet_renames_its_variable(self):
        x = fake_x(answers={"https://api.x.com/graphql/": {"data": {"unretweet": {"source_tweet_results": {}}}}})
        with patch.object(api.transport, "request", x), contextlib.redirect_stdout(io.StringIO()):
            engagement.unrt(["77"])
        self.assertEqual(x.call_args.args[1], "https://api.x.com/graphql/unrt-id/retweetButtonUnretweetMutation")
        self.assertEqual(json.loads(x.call_args.kwargs["data"])["variables"], {"tweetId": "77"})

    def test_follow_goes_to_api_x_com_and_needs_the_user_back(self):
        ok = fake_x(answers={"https://api.x.com/1.1/friendships/create.json": {"id_str": "42"}})
        with patch.object(api.transport, "request", ok):
            api.rest_post("/1.1/friendships/create.json", {"user_id": "42"}, form=True)
        self.assertEqual(ok.call_args.args, ("POST", "https://api.x.com/1.1/friendships/create.json"))
        self.assertEqual(ok.call_args.kwargs["data"], b"user_id=42")
        empty = fake_x(answers={"https://api.x.com/1.1/friendships/create.json": {}})
        with patch.object(api.transport, "request", empty), self.assertRaisesRegex(api.TwitterAPIError, "Outcome unclear"):
            api.rest_post("/1.1/friendships/create.json", {"user_id": "42"}, form=True)

    def test_writes_not_ported_to_x_web_stay_paused(self):
        x = fake_x()
        with patch.object(api.transport, "request", x):
            with self.assertRaisesRegex(write_guard.WritePaused, "CreateTweet is not ported"):
                posting.graphql_post("q", "CreateTweet", {"tweet_text": "x"})
            with self.assertRaisesRegex(write_guard.WritePaused, "not ported"):
                api.rest_post("/1.1/dm/new2.json", {"text": "x"})
        x.assert_not_called()

    def test_doctor_reports_ported_writes_allowed(self):
        from src import doctor

        out = io.StringIO()
        with (
            patch.object(doctor, "get_tokens", return_value=TOKENS),
            patch.object(doctor, "self_check", return_value=([], {**self.live, "fetched_at": time.time()})),
            contextlib.redirect_stdout(out),
        ):
            doctor.doctor([])
        self.assertIn("Writes: allowed", out.getvalue())
        self.assertIn("x-web", out.getvalue())
        self.assertIn("(paused): CreateTweet, DeleteTweet, DMs", out.getvalue())


if __name__ == "__main__":
    unittest.main()
