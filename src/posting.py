"""Posting commands — post, reply, delete."""

import argparse
import sys

from src.api import graphql_post
from src.parse import parse_tweet
from src.format import format_tweet, format_json
from src.helpers import Q, require_tweet_ref


def post(argv):
    """Post a new tweet, optionally as a reply or quote."""
    p = argparse.ArgumentParser(prog="twitter post")
    p.add_argument("text", nargs="+", help="Tweet text")
    p.add_argument("-r", "--reply", type=str, help="Tweet ID/URL to reply to")
    p.add_argument("-q", "--quote", type=str, help="Tweet ID/URL to quote")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    args = p.parse_args(argv)

    text = " ".join(args.text)
    if not text:
        print("Error: tweet text is required.", file=sys.stderr)
        sys.exit(1)

    variables = {
        "tweet_text": text,
        "dark_request": False,
        "media": {"media_entities": [], "possibly_sensitive": False},
        "semantic_annotation_ids": [],
    }

    if args.reply:
        tweet_id = require_tweet_ref(args.reply)
        variables["reply"] = {
            "in_reply_to_tweet_id": tweet_id,
            "exclude_reply_user_ids": [],
        }

    if args.quote:
        tweet_id = require_tweet_ref(args.quote)
        variables["attachment_url"] = f"https://x.com/i/status/{tweet_id}"

    data = graphql_post(Q["CreateTweet"], "CreateTweet", variables)

    result = (
        data.get("data", {})
        .get("create_tweet", {})
        .get("tweet_results", {})
        .get("result", {})
    )
    tweet = parse_tweet(result)

    # Check for errors in the response
    errors = data.get("errors", [])
    if errors:
        for err in errors:
            print(f"Error: {err.get('message', 'Unknown error')}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(format_json(tweet or data))
    elif tweet:
        print("Posted:")
        print(format_tweet(tweet))
    else:
        fallback_id = (
            data.get("data", {})
            .get("create_tweet", {})
            .get("tweet_results", {})
            .get("result", {})
            .get("rest_id")
        )
        if fallback_id:
            print(f"Posted. Tweet ID: {fallback_id}")
        else:
            print("Posted.")


def reply(argv):
    """Reply to a tweet. Convenience wrapper around post."""
    p = argparse.ArgumentParser(prog="twitter reply")
    p.add_argument("tweet", help="Tweet ID or URL to reply to")
    p.add_argument("text", nargs="+", help="Reply text")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    args = p.parse_args(argv)

    # Rewrite as a post --reply call
    post_argv = args.text + ["--reply", args.tweet]
    if args.json:
        post_argv.append("--json")
    post(post_argv)


def delete(argv):
    """Delete one of your own tweets."""
    p = argparse.ArgumentParser(prog="twitter delete")
    p.add_argument("tweet", help="Tweet ID or URL")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    args = p.parse_args(argv)

    tweet_id = require_tweet_ref(args.tweet)
    graphql_post(
        Q["DeleteTweet"], "DeleteTweet",
        {"tweet_id": tweet_id, "dark_request": False},
    )
    if args.json:
        print(format_json({"deleted": tweet_id}))
    else:
        print(f"Deleted.")
