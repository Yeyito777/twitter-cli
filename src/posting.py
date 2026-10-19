"""Posting commands — post, reply, delete."""

import argparse
import sys

from src.api import graphql_post
from src.agent_tweets import record_agent_tweet, unrecord_agent_tweet
from src.parse import parse_tweet
from src.format import format_tweet
from src.helpers import Q, require_tweet_ref
from src.payload import read_stdin_text, reject_inline_text


def post(argv):
    """Post a new tweet, optionally as a reply or quote."""
    p = argparse.ArgumentParser(
        allow_abbrev=False,
        prog="twitter post",
        description="Post a tweet using text read exactly from stdin.",
        epilog="Inline tweet text is not accepted; stdin is required.",
    )
    p.add_argument("inline_text", nargs="*", help=argparse.SUPPRESS)
    p.add_argument("-r", "--reply", type=str, help="Tweet ID/URL to reply to")
    p.add_argument("-q", "--quote", type=str, help="Tweet ID/URL to quote")
    args = p.parse_args(argv)

    reject_inline_text(p, args.inline_text, label="tweet text")
    text = read_stdin_text(p, label="tweet text")
    _post_text(text, reply=args.reply, quote=args.quote)


def _post_text(text, *, reply=None, quote=None):
    """Post already-decoded exact text with optional reply/quote metadata."""

    variables = {
        "tweet_text": text,
        "dark_request": False,
        "media": {"media_entities": [], "possibly_sensitive": False},
        "semantic_annotation_ids": [],
    }

    reply_to_id = None
    quote_id = None

    if reply:
        tweet_id = require_tweet_ref(reply)
        reply_to_id = tweet_id
        variables["reply"] = {
            "in_reply_to_tweet_id": tweet_id,
            "exclude_reply_user_ids": [],
        }

    if quote:
        tweet_id = require_tweet_ref(quote)
        quote_id = tweet_id
        variables["attachment_url"] = f"https://x.com/i/status/{tweet_id}"

    data = graphql_post(Q["CreateTweet"], "CreateTweet", variables)

    result = (
        data.get("data", {})
        .get("create_tweet", {})
        .get("tweet_results", {})
        .get("result", {})
    )
    parsed = parse_tweet(result)

    # Check for errors in the response
    errors = data.get("errors", [])
    if errors:
        for err in errors:
            print(f"Error: {err.get('message', 'Unknown error')}", file=sys.stderr)
        sys.exit(1)

    if parsed:
        record_agent_tweet(
            parsed.get("id"),
            text=parsed.get("text", text),
            command="reply" if reply_to_id else "post",
            reply_to_id=reply_to_id,
            quote_id=quote_id,
            url=parsed.get("url", ""),
        )
        print("Posted:")
        print(format_tweet(parsed))
    else:
        fallback_id = (
            data.get("data", {})
            .get("create_tweet", {})
            .get("tweet_results", {})
            .get("result", {})
            .get("rest_id")
        )
        if fallback_id:
            record_agent_tweet(
                fallback_id,
                text=text,
                command="reply" if reply_to_id else "post",
                reply_to_id=reply_to_id,
                quote_id=quote_id,
            )
            print(f"Posted. Tweet ID: {fallback_id}")
        else:
            print("Posted.")


def reply(argv):
    """Reply to a tweet using exact text from stdin."""
    p = argparse.ArgumentParser(
        allow_abbrev=False,
        prog="twitter reply",
        description="Reply to a tweet using text read exactly from stdin.",
        epilog="Inline reply text is not accepted; stdin is required.",
    )
    p.add_argument("tweet", help="Tweet ID or URL to reply to")
    p.add_argument("inline_text", nargs="*", help=argparse.SUPPRESS)
    args = p.parse_args(argv)

    reject_inline_text(p, args.inline_text, label="reply text")
    text = read_stdin_text(p, label="reply text")
    _post_text(text, reply=args.tweet)


def delete(argv):
    """Delete one of your own tweets."""
    p = argparse.ArgumentParser(allow_abbrev=False, prog="twitter delete")
    p.add_argument("tweet", help="Tweet ID or URL")
    args = p.parse_args(argv)

    tweet_id = require_tweet_ref(args.tweet)
    graphql_post(
        Q["DeleteTweet"], "DeleteTweet",
        {"tweet_id": tweet_id, "dark_request": False},
    )
    unrecord_agent_tweet(tweet_id)
    print("Deleted.")
