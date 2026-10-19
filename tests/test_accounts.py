import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src import accounts, agent_tweets, auth, notify


BIN = Path(__file__).resolve().parents[1] / "bin" / "twitter"
MAIN = {"user_id": "111", "screen_name": "main_user", "name": "Main", "owner": "user",
        "notify_source": notify.NOTIFICATION_SOURCE_ID}
ALT = {"user_id": "222", "screen_name": "alt_user", "name": "Alt", "owner": "assistant"}
TOKENS = {"auth_token": "a" * 40, "ct0": "c" * 32}


class AccountTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        for item in (
            patch.object(accounts, "CONFIG_ROOT", self.root),
            patch.dict(os.environ, {}),
        ):
            item.start()
            self.addCleanup(item.stop)
        os.environ.pop(accounts.ACCOUNT_ENV, None)
        os.environ.pop(accounts.ACCOUNT_EXPLICIT_ENV, None)
        self.addCleanup(self.temp_dir.cleanup)

    def write_registry(self, accounts_meta, default=None):
        accounts.save_registry({"version": 1, "default_account": default, "accounts": accounts_meta})
        for alias in accounts_meta:
            auth.save_tokens(TOKENS, alias)

    def select(self, alias):
        os.environ[accounts.ACCOUNT_ENV] = alias
        os.environ[accounts.ACCOUNT_EXPLICIT_ENV] = "1"


class SelectionTests(AccountTestCase):
    def test_unmigrated_install_uses_legacy_paths(self):
        self.assertIsNone(accounts.selected_alias())
        self.assertEqual(accounts.account_dir(), self.root)
        self.assertEqual(notify.notification_source()["id"], notify.NOTIFICATION_SOURCE_ID)

    def test_default_explicit_and_unknown_accounts(self):
        self.write_registry({"main": MAIN, "alt": ALT}, default="main")
        self.assertEqual(accounts.selected_alias(), "main")
        self.select("alt")
        self.assertEqual(accounts.selected_alias(), "alt")
        self.assertEqual(accounts.account_dir(), self.root / "accounts" / "alt")
        self.select("nope")
        with self.assertRaisesRegex(accounts.AccountError, "not configured"):
            accounts.selected_alias()

    def test_several_accounts_without_a_default_need_a_selection(self):
        self.write_registry({"main": MAIN, "alt": ALT})
        with self.assertRaisesRegex(accounts.AccountError, "No default"):
            accounts.selected_alias()

    def test_tokens_and_managed_tweets_are_kept_per_account(self):
        self.write_registry({"main": MAIN, "alt": ALT}, default="main")
        auth.save_tokens({"auth_token": "alt-token", "ct0": "alt-csrf"}, "alt")
        agent_tweets.record_agent_tweet("1001", account="alt")

        self.assertEqual(auth.get_tokens()["auth_token"], TOKENS["auth_token"])
        self.assertEqual(auth.get_tokens("alt")["auth_token"], "alt-token")
        self.assertTrue(agent_tweets.is_agent_tweet("1001", "alt"))
        self.assertFalse(agent_tweets.is_agent_tweet("1001"))
        mode = stat.S_IMODE((self.root / "accounts" / "alt" / "credentials.json").stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_migrated_login_keeps_its_source_and_new_accounts_get_their_own(self):
        self.write_registry({"main": MAIN, "alt": ALT}, default="main")
        self.assertEqual(notify.notification_source("main")["id"], notify.NOTIFICATION_SOURCE_ID)
        alt = notify.notification_source("alt")
        self.assertEqual(alt["id"], f"account:alt:{notify.NOTIFICATION_SOURCE_ID}")
        self.assertIn("@alt_user", alt["label"])

    def test_notify_polls_only_logged_in_accounts(self):
        self.write_registry({"main": MAIN, "alt": ALT}, default="main")
        (self.root / "accounts" / "alt" / "credentials.json").unlink()
        self.assertEqual(accounts.notify_aliases(), ["main"])


class PreflightTests(AccountTestCase):
    def preflight(self, cmd, argv):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            accounts.preflight(cmd, argv)
        return stderr.getvalue()

    def test_single_account_reads_need_no_selection(self):
        self.write_registry({"main": MAIN})
        self.assertEqual(self.preflight("timeline", []), "")

    def test_single_account_writes_still_require_an_explicit_account(self):
        # The only account may be the user's own: never post as it by default.
        self.write_registry({"main": MAIN})
        for cmd, argv in (("post", []), ("like", ["1"]), ("dm", ["@x", "--send"]), ("follow", ["x"])):
            with self.subTest(cmd=cmd), self.assertRaisesRegex(accounts.AccountError, "even with one account"):
                accounts.preflight(cmd, argv)

    def test_mutations_require_an_explicit_account_when_several_exist(self):
        self.write_registry({"main": MAIN, "alt": ALT}, default="main")
        for cmd, argv in (
            ("post", []), ("like", ["1"]), ("login", []),
            ("dm", ["@x", "--send"]), ("dm", ["@x", "--sen"]), ("dm", ["-s", "@x"]),
            ("notify", ["subscribe", "conv"]), ("notify", ["mark", "1"]),
        ):
            with self.subTest(cmd=cmd, argv=argv), self.assertRaisesRegex(accounts.AccountError, "-a/--account"):
                accounts.preflight(cmd, argv)

    def test_reads_use_the_default_and_name_it(self):
        self.write_registry({"main": MAIN, "alt": ALT}, default="main")
        for cmd, argv in (("timeline", []), ("dm", ["@x"]), ("notify", ["list"]), ("notify", ["start"])):
            with self.subTest(cmd=cmd):
                self.assertIn("main — Main (@main_user) [user]", self.preflight(cmd, argv))

    def test_explicit_account_is_validated_and_named(self):
        self.write_registry({"main": MAIN, "alt": ALT}, default="main")
        self.select("alt")
        self.assertIn("alt — Alt (@alt_user) [assistant]", self.preflight("post", []))
        self.select("ghost")
        with self.assertRaisesRegex(accounts.AccountError, "not configured"):
            accounts.preflight("timeline", [])


class AccountCommandTests(AccountTestCase):
    def run_quietly(self, fn, argv, stdin=""):
        with (
            patch.object(auth.sys, "stdin", io.StringIO(stdin)),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            fn(argv)

    def identity(self, meta):
        return {k: meta[k] for k in ("user_id", "screen_name", "name")}

    def test_migrate_moves_legacy_files_and_keeps_the_original_source(self):
        (self.root / "credentials.json").write_text(json.dumps(TOKENS))
        (self.root / "agent-tweets.json").write_text(json.dumps({"managed_tweet_ids": ["5"]}))
        (self.root / "notify-state.json").write_text("{}")
        (self.root / "notify.json").write_text("{}")
        with (
            patch.object(notify, "find_notify_pids", return_value=[]),
            patch.object(auth, "verify_identity", return_value=self.identity(MAIN)) as verify,
        ):
            self.run_quietly(accounts.migrate_legacy, ["main", "--handle", "main_user", "--owner", "user"])

        verify.assert_called_once()
        target = self.root / "accounts" / "main"
        for name in accounts.ACCOUNT_FILES:
            self.assertTrue((target / name).exists(), name)
            self.assertFalse((self.root / name).exists(), name)
        self.assertTrue((self.root / "notify.json").exists())
        registry = accounts.load_registry()
        self.assertEqual(registry["default_account"], "main")
        self.assertEqual(registry["accounts"]["main"]["notify_source"], notify.NOTIFICATION_SOURCE_ID)
        self.assertTrue(agent_tweets.is_agent_tweet("5"))

    def test_migrate_refuses_while_the_poller_runs(self):
        (self.root / "credentials.json").write_text(json.dumps(TOKENS))
        with (
            patch.object(notify, "find_notify_pids", return_value=[42]),
            self.assertRaisesRegex(accounts.AccountError, "notify stop"),
        ):
            accounts.migrate_legacy(["main", "--handle", "main_user", "--owner", "user"])
        self.assertTrue((self.root / "credentials.json").exists())

    def test_add_requires_naming_the_legacy_login_first(self):
        (self.root / "credentials.json").write_text(json.dumps(TOKENS))
        with self.assertRaisesRegex(accounts.AccountError, "migrate"):
            accounts.add_account(["alt", "--handle", "alt_user", "--owner", "assistant", "--stdin"])

    def test_add_verifies_and_stores_a_second_login(self):
        self.write_registry({"main": MAIN}, default="main")
        with patch.object(auth, "verify_identity", return_value=self.identity(ALT)) as verify:
            self.run_quietly(
                accounts.add_account,
                ["alt", "--handle", "@alt_user", "--owner", "assistant", "--stdin"],
                stdin=json.dumps({"auth_token": "alt-token", "ct0": "alt-csrf"}),
            )

        self.assertEqual(verify.call_args.args[0]["auth_token"], "alt-token")
        self.assertEqual(verify.call_args.kwargs, {"handle": "@alt_user"})
        registry = accounts.load_registry()
        self.assertEqual(registry["default_account"], "main")
        self.assertEqual(registry["accounts"]["alt"], ALT)
        self.assertEqual(auth.get_tokens("alt")["ct0"], "alt-csrf")

    def test_add_refuses_a_handle_that_is_already_configured(self):
        self.write_registry({"main": MAIN}, default="main")
        with self.assertRaisesRegex(accounts.AccountError, "already configured as 'main'"):
            accounts.add_account(["again", "--handle", "@Main_User", "--owner", "user", "--stdin"])

    def test_login_refuses_another_users_credentials(self):
        self.write_registry({"main": MAIN, "alt": ALT}, default="main")
        self.select("alt")
        with (
            patch.object(auth, "viewer_id", return_value="111"),
            patch.object(auth.sys, "stdin", io.StringIO(json.dumps({"auth_token": "x", "ct0": "y"}))),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            auth.login(["--stdin"])
        self.assertIn("not 'alt'", stderr.getvalue())
        self.assertEqual(auth.get_tokens("alt")["auth_token"], TOKENS["auth_token"])

    def test_remove_drops_files_subscriptions_and_default(self):
        self.write_registry({"main": MAIN, "alt": ALT}, default="alt")
        unsubscribe = Mock()
        with (
            patch("src.exocortex.list_external_notification_subscriptions", return_value=[{"id": "sub-1"}]),
            patch("src.exocortex.unsubscribe_external_notification", unsubscribe),
        ):
            self.run_quietly(accounts.remove_account, ["alt", "--yes"])

        unsubscribe.assert_called_once_with(subscription_id="sub-1")
        self.assertFalse((self.root / "accounts" / "alt").exists())
        registry = accounts.load_registry()
        self.assertEqual(sorted(registry["accounts"]), ["main"])
        self.assertEqual(registry["default_account"], "main")


class VimbrowserImportTests(AccountTestCase):
    def cookies(self, **values):
        return subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"cookies": [{"name": k, "value": v} for k, v in values.items()]}),
        )

    def browser(self):
        return self.cookies(
            twid="u%3D111", auth_token="active-token", ct0="active-csrf",
            auth_multi='"222:alt-token|333:third-token"',
        )

    def test_active_login_uses_the_browser_csrf_token(self):
        with patch.object(auth.subprocess, "run", return_value=self.browser()):
            tokens = auth.vimbrowser_credentials(user_id="111")
        self.assertEqual((tokens["auth_token"], tokens["ct0"]), ("active-token", "active-csrf"))

    def test_other_logins_get_their_own_csrf_token(self):
        with (
            patch.object(auth.subprocess, "run", return_value=self.browser()),
            patch.object(auth, "_issue_ct0", return_value="alt-csrf") as issue,
        ):
            tokens = auth.vimbrowser_credentials(user_id="222")
        issue.assert_called_once_with("alt-token")
        self.assertEqual((tokens["auth_token"], tokens["ct0"]), ("alt-token", "alt-csrf"))

    def test_handle_is_matched_by_the_login_that_owns_it(self):
        def lookup(handle, tokens):
            # Other logins see a suspended account as unavailable; its owner sees itself.
            if tokens["auth_token"] != "third-token":
                raise accounts.AccountError("User @third is unavailable: Suspended.")
            return {"user_id": "333"}

        with (
            patch.object(auth.subprocess, "run", return_value=self.browser()),
            patch.object(auth, "lookup_user", side_effect=lookup),
            patch.object(auth, "_issue_ct0", side_effect=lambda token: token.replace("token", "csrf")),
        ):
            tokens = auth.vimbrowser_credentials(handle="third")
        self.assertEqual((tokens["auth_token"], tokens["ct0"]), ("third-token", "third-csrf"))

    def test_missing_login_is_reported(self):
        with (
            patch.object(auth.subprocess, "run", return_value=self.browser()),
            self.assertRaisesRegex(accounts.AccountError, "not logged in to X user 999"),
        ):
            auth.vimbrowser_credentials(user_id="999")
        with (
            patch.object(auth.subprocess, "run", return_value=self.browser()),
            patch.object(auth, "lookup_user", return_value={"user_id": "444"}),
            patch.object(auth, "_issue_ct0", return_value="csrf"),
            self.assertRaisesRegex(accounts.AccountError, "not logged in to @nobody"),
        ):
            auth.vimbrowser_credentials(handle="@nobody")


class NotifyDaemonAccountTests(AccountTestCase):
    def test_daemon_follows_accounts_and_gives_each_its_own_source(self):
        self.write_registry({"main": MAIN, "alt": ALT}, default="main")
        daemon = object.__new__(notify.NotifyDaemon)
        daemon.services = {}
        daemon.poll_seconds = 300
        daemon.max_parent_replies = 10
        daemon._log = Mock()
        with patch.object(notify, "register_external_notification_source") as register:
            daemon._sync_services()
            self.assertEqual(sorted(daemon.services), ["alt", "main"])
            self.assertEqual(
                sorted(call.args[1] for call in register.call_args_list),
                [f"account:alt:{notify.NOTIFICATION_SOURCE_ID}", notify.NOTIFICATION_SOURCE_ID],
            )
            self.assertEqual(daemon.services["alt"].state_file, self.root / "accounts" / "alt" / "notify-state.json")

            (self.root / "accounts" / "alt" / "credentials.json").unlink()
            daemon._sync_services()
        self.assertEqual(sorted(daemon.services), ["main"])

    def test_relay_names_the_account_to_act_as(self):
        self.write_registry({"main": MAIN, "alt": ALT}, default="main")
        service = notify.TwitterNotifyService("alt", Mock(), poll_seconds=300, max_parent_replies=10)
        tweet = {"id": "9", "handle": "someone", "name": "Someone", "text": "hi", "created_at": "",
                 "is_retweet": False, "is_quote": False, "is_reply": False, "in_reply_to": None,
                 "likes": 0, "retweets": 0, "replies": 0, "views": 0, "media": []}
        message = service._format_event_message({
            "event_type": "quote_tweet", "priority": "high", "reason": "r", "source": "s",
            "incoming_tweet": tweet, "parent_tweet": tweet,
        })
        self.assertIn("Account: alt — Alt (@alt_user) [assistant]", message)
        self.assertIn("pass `-a alt` to every twitter command", message)


class CommandLineTests(AccountTestCase):
    def run_bin(self, *argv):
        env = {**os.environ, "TWITTER_CONFIG_DIR": str(self.root)}
        env.pop(accounts.ACCOUNT_ENV, None)
        env.pop(accounts.ACCOUNT_EXPLICIT_ENV, None)
        return subprocess.run([str(BIN), *argv], input="", text=True, capture_output=True, env=env)

    def test_account_flag_is_accepted_before_or_after_the_command(self):
        self.write_registry({"main": MAIN, "alt": ALT}, default="main")
        refused = self.run_bin("post")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("requires '-a/--account <alias>'", refused.stderr)
        for argv in (("-a", "alt", "post"), ("post", "--account", "alt"), ("post", "--account=alt")):
            with self.subTest(argv=argv):
                result = self.run_bin(*argv)
                # Account accepted; the empty payload is the next thing rejected.
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("Twitter account: alt", result.stderr)
                self.assertIn("tweet text is required on stdin", result.stderr)

    def test_accounts_lists_configured_logins(self):
        self.write_registry({"main": MAIN, "alt": ALT}, default="main")
        output = self.run_bin("accounts").stdout
        self.assertIn("main — Main (@main_user) [user]; default", output)
        self.assertIn("alt — Alt (@alt_user) [assistant]", output)


if __name__ == "__main__":
    unittest.main()
