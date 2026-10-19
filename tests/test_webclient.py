import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import bs4
from x_client_transaction import ClientTransaction

from src import accounts, fingerprint, transport, webclient

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HOME = (FIXTURES / "x_home.html").read_text()
ONDEMAND = (FIXTURES / "ondemand.js").read_text()
MAIN_JS = (
    'e.exports={queryId:"live-create",operationName:"CreateTweet",operationType:"mutation",'
    'metadata:{featureSwitches:["f_on","f_off","f_missing"],fieldToggles:["withX"]}};'
    'e.exports={queryId:"live-detail",operationName:"TweetDetail",operationType:"query",'
    'metadata:{featureSwitches:["f_on"],fieldToggles:[]}}'
)
HOME_JS = (
    'e.exports={queryId:"live-home",operationName:"HomeTimeline",operationType:"query",'
    'metadata:{featureSwitches:["f_on"],fieldToggles:[]}}'
)
TOKENS = {"auth_token": "a", "ct0": "c", "bearer": "b", "alias": "main"}


def fake_x(page_status=200, page_url="https://x.com/home", page=HOME):
    """transport.request standing in for x.com and abs.twimg.com."""
    scripts = {
        "main.0123abcd.js": MAIN_JS,
        "ondemand.s.81a348d6aed80cada.js": ONDEMAND,
        "bundle.HomeTimeline.aa11a.js": "",
        "shared~bundle.Compose~bundle.HomeTimeline.bb22a.js": HOME_JS,
    }

    def request(method, url, *, headers, **_kwargs):
        if url == webclient.HOME_URL:
            return transport.Response(page_status, page.encode(), page_url)
        name = url.rsplit("/", 1)[1]
        if url.startswith(webclient.ASSET_BASE) and name in scripts:
            return transport.Response(200, scripts[name].encode(), url)
        return transport.Response(404, b"", url)

    return Mock(side_effect=request)


class _Account(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "accounts" / "main").mkdir(parents=True)
        (root / "accounts.json").write_text(json.dumps({"version": 1, "default_account": "main", "accounts": {
            "main": {"user_id": "1", "screen_name": "m", "name": "M", "owner": "user"}}}))
        for item in (
            patch.object(accounts, "CONFIG_ROOT", root),
            patch.dict(os.environ, {"XDG_CACHE_HOME": str(root / "cache")}),
        ):
            item.start()
            self.addCleanup(item.stop)
        webclient._memory.clear()
        self.addCleanup(webclient._memory.clear)


class FetchTests(_Account):
    def test_snapshot_from_the_saved_page(self):
        with patch.object(transport, "request", fake_x()) as x:
            snapshot = webclient.fetch(TOKENS)
        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["client"], "main.0123abcd.js")
        self.assertEqual(webclient.operation(snapshot, "CreateTweet"),
                         ("live-create", {"f_on": True, "f_off": False, "f_missing": False}))
        self.assertEqual(webclient.operation(snapshot, "HomeTimeline")[0], "live-home")
        self.assertIsNone(webclient.operation(snapshot, "Nope"))
        page_headers = dict(x.call_args_list[0].kwargs["headers"])
        self.assertEqual(page_headers["user-agent"], fingerprint.USER_AGENT)
        self.assertEqual(page_headers["cookie"], "auth_token=a; ct0=c")
        self.assertEqual(page_headers["sec-fetch-mode"], "navigate")

    def test_transaction_id_matches_the_reference_algorithm(self):
        with patch.object(transport, "request", fake_x()):
            snapshot = webclient.fetch(TOKENS)
        reference = ClientTransaction(
            home_page_response=bs4.BeautifulSoup(HOME, "html.parser"),
            ondemand_file_response=ONDEMAND,
        )
        now = 1_792_400_000
        with patch("x_client_transaction.transaction.random.randint", return_value=77):
            expected = reference.generate_transaction_id(
                "POST", "/i/api/graphql/live-create/CreateTweet", time_now=now - webclient.TXID_EPOCH)
        ours = webclient.transaction_id(snapshot, "POST", "/i/api/graphql/live-create/CreateTweet",
                                        now=now, random_byte=77)
        self.assertEqual(ours, expected)
        self.assertNotEqual(ours, webclient.transaction_id(snapshot, "GET", "/other", now=now, random_byte=77))

    def test_login_redirect_is_a_failure(self):
        with patch.object(transport, "request", fake_x(page_url="https://x.com/i/flow/login")):
            with self.assertRaisesRegex(webclient.WebClientError, "not accepted"):
                webclient.fetch(TOKENS)

    def test_missing_ondemand_chunk_is_a_failure(self):
        broken = HOME.replace('"ondemand.s"', '"renamed.s"')
        with patch.object(transport, "request", fake_x(page=broken)):
            with self.assertRaisesRegex(webclient.WebClientError, "ondemand.s"):
                webclient.fetch(TOKENS)


class CacheTests(_Account):
    def test_snapshot_is_cached_on_disk_and_reused(self):
        with patch.object(transport, "request", fake_x()) as x:
            first = webclient.load(TOKENS, "main")
            webclient._memory.clear()
            second = webclient.load(TOKENS, "main")
        self.assertEqual(first, second)
        page_loads = [c for c in x.call_args_list if c.args[1] == webclient.HOME_URL]
        self.assertEqual(len(page_loads), 1)

    def test_scripts_are_fetched_once(self):
        with patch.object(transport, "request", fake_x()) as x:
            webclient.load(TOKENS, "main")
            webclient.load(TOKENS, "main", refresh=True)
        scripts = [c for c in x.call_args_list if c.args[1].startswith(webclient.ASSET_BASE)]
        self.assertEqual(len(scripts), 4)

    def test_failure_is_cached_instead_of_refetching_before_every_call(self):
        with patch.object(transport, "request", fake_x(page_status=503)) as x:
            for _ in range(3):
                with self.assertRaisesRegex(webclient.WebClientError, "503"):
                    webclient.load(TOKENS, "main")
        self.assertEqual(x.call_count, 1)

    def test_stale_snapshot_is_refreshed(self):
        with patch.object(transport, "request", fake_x()) as x:
            webclient.load(TOKENS, "main")
            with patch.object(webclient.time, "time", return_value=time.time() + webclient.SNAPSHOT_TTL + 1):
                webclient.load(TOKENS, "main")
        page_loads = [c for c in x.call_args_list if c.args[1] == webclient.HOME_URL]
        self.assertEqual(len(page_loads), 2)
