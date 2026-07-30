import argparse
import contextlib
import io
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src import posting, reading
from src.payload import read_stdin_text


BIN = Path(__file__).resolve().parents[1] / "bin" / "twitter"


class _BytesStdin:
    def __init__(self, value):
        self.buffer = io.BytesIO(value)

    def isatty(self):
        return False


class PayloadHelperTests(unittest.TestCase):
    def test_preserves_exact_utf8_without_trimming_or_escape_decoding(self):
        payload = "  `$HOME` ${USER} \\n\n```json\n{}\n``` 👁️  \n"
        parser = argparse.ArgumentParser()
        with patch("src.payload.sys.stdin", _BytesStdin(payload.encode("utf-8"))):
            self.assertEqual(read_stdin_text(parser, label="tweet text"), payload)

    def test_rejects_invalid_utf8(self):
        parser = argparse.ArgumentParser(prog="twitter post")
        stderr = io.StringIO()
        with (
            patch("src.payload.sys.stdin", _BytesStdin(b"\xff")),
            contextlib.redirect_stderr(stderr),
            self.assertRaisesRegex(SystemExit, "2"),
        ):
            read_stdin_text(parser, label="tweet text")
        self.assertIn("tweet text on stdin must be valid UTF-8", stderr.getvalue())


class PostingPayloadTests(unittest.TestCase):
    def test_post_passes_exact_stdin_text_to_api(self):
        payload = "  multiline `$HOME`\nsecond line 👁️  \n"
        graphql = Mock(return_value={
            "data": {"create_tweet": {"tweet_results": {"result": {"rest_id": "1"}}}}
        })
        with (
            patch.object(posting, "graphql_post", graphql),
            patch.object(posting, "parse_tweet", return_value=None),
            patch.object(posting, "record_agent_tweet"),
            patch.object(posting.sys, "stdin", io.StringIO(payload)),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            posting.post([])

        variables = graphql.call_args.args[2]
        self.assertEqual(variables["tweet_text"], payload)

    def test_reply_keeps_target_in_argv_and_text_on_stdin(self):
        payload = "reply text\n"
        post_text = Mock()
        with (
            patch.object(posting, "_post_text", post_text),
            patch.object(posting.sys, "stdin", io.StringIO(payload)),
        ):
            posting.reply(["123456"])
        post_text.assert_called_once_with(payload, reply="123456")

    def test_inline_and_missing_post_payloads_fail_before_api_access(self):
        for argv, expected in (
            (["inline", "tweet"], "tweet text must be provided via stdin"),
            ([], "tweet text is required on stdin"),
        ):
            result = subprocess.run(
                [str(BIN), "post", *argv],
                input="",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn(expected, result.stderr)
            self.assertNotIn("credentials", result.stderr.lower())

    def test_inline_and_missing_reply_payloads_fail_before_api_access(self):
        for argv, expected in (
            (["123456", "inline", "reply"], "reply text must be provided via stdin"),
            (["123456"], "reply text is required on stdin"),
        ):
            result = subprocess.run(
                [str(BIN), "reply", *argv],
                input="",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn(expected, result.stderr)
            self.assertNotIn("credentials", result.stderr.lower())


class DirectMessagePayloadTests(unittest.TestCase):
    def test_dm_send_passes_exact_stdin_text(self):
        payload = "  DM `$HOME`\nline two 👁️  \n"
        send = Mock()
        with (
            patch.object(reading, "_dm_send", send),
            patch.object(reading.sys, "stdin", io.StringIO(payload)),
        ):
            reading.dm(["@recipient", "--send"])
        send.assert_called_once_with("@recipient", payload)

    def test_dm_send_rejects_inline_and_missing_payloads(self):
        for args, expected in (
            (["@recipient", "--send", "inline"], "DM text must be provided via stdin"),
            (["@recipient", "--send"], "DM text is required on stdin"),
        ):
            result = subprocess.run(
                [str(BIN), "dm", *args],
                input="",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn(expected, result.stderr)
            self.assertNotIn("credentials", result.stderr.lower())


class PayloadHelpTests(unittest.TestCase):
    def test_top_level_and_command_help_document_stdin_contract(self):
        top = subprocess.run([str(BIN), "-h"], text=True, capture_output=True)
        post = subprocess.run([str(BIN), "post", "-h"], text=True, capture_output=True)
        reply = subprocess.run([str(BIN), "reply", "-h"], text=True, capture_output=True)
        dm = subprocess.run([str(BIN), "dm", "-h"], text=True, capture_output=True)

        self.assertIn("read exact UTF-8 text from stdin", top.stdout)
        self.assertIn("Inline tweet text is not accepted", post.stdout)
        self.assertIn("Inline reply text is not accepted", reply.stdout)
        self.assertIn("text read exactly from stdin", dm.stdout)


if __name__ == "__main__":
    unittest.main()
