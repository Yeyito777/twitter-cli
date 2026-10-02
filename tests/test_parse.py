import unittest

from src.parse import parse_tweet


class TweetAuthorTests(unittest.TestCase):
    def _result(self):
        return {
            "__typename": "Tweet",
            "rest_id": "tweet-1",
            "legacy": {"full_text": "hello", "user_id_str": "legacy-author"},
            "core": {"user_results": {"result": {
                "rest_id": "author-1",
                "core": {"name": "Author", "screen_name": "author"},
            }}},
        }

    def test_preserves_author_id_from_user_result(self):
        self.assertEqual(parse_tweet(self._result())["author_id"], "author-1")

    def test_legacy_author_id_is_used_when_user_result_is_missing(self):
        result = self._result()
        result.pop("core")
        self.assertEqual(parse_tweet(result)["author_id"], "legacy-author")

    def test_missing_author_id_is_not_inferred_from_handle(self):
        result = self._result()
        del result["core"]["user_results"]["result"]["rest_id"]
        del result["legacy"]["user_id_str"]
        self.assertEqual(parse_tweet(result)["author_id"], "")

    def test_visibility_wrapper_and_nested_quotes_keep_author_ids(self):
        result = self._result()
        result["legacy"]["is_quote_status"] = True
        result["quoted_status_result"] = {"result": self._result()}
        tweet = parse_tweet({"__typename": "TweetWithVisibilityResults", "tweet": result})
        self.assertEqual(tweet["author_id"], "author-1")
        self.assertEqual(tweet["quoted"]["author_id"], "author-1")


if __name__ == "__main__":
    unittest.main()
