"""Posting commands — post, reply, delete."""

import argparse
import sys

from src.api import graphql_post
from src.agent_tweets import record_agent_tweet, unrecord_agent_tweet
from src.parse import parse_tweet
from src.format import format_tweet
from src.helpers import Q, require_tweet_ref


def post(argv):
    """Post a new tweet, optionally as a reply or quote."""
    p = argparse.ArgumentParser(prog="twitter post")
    p.add_argument("text", nargs="+", help="Tweet text")
    p.add_argument("-r", "--reply", type=str, help="Tweet ID/URL to reply to")
    p.add_argument("-q", "--quote", type=str, help="Tweet ID/URL to quote")
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

    reply_to_id = None
    quote_id = None

    if args.reply:
        tweet_id = require_tweet_ref(args.reply)
        reply_to_id = tweet_id
        variables["reply"] = {
            "in_reply_to_tweet_id": tweet_id,
            "exclude_reply_user_ids": [],
        }

    if args.quote:
        tweet_id = require_tweet_ref(args.quote)
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
    """Reply to a tweet. Convenience wrapper around post."""
    p = argparse.ArgumentParser(prog="twitter reply")
    p.add_argument("tweet", help="Tweet ID or URL to reply to")
    p.add_argument("text", nargs="+", help="Reply text")
    args = p.parse_args(argv)

    # Rewrite as a post --reply call
    post(args.text + ["--reply", args.tweet])


def delete(argv):
    """Delete one of your own tweets."""
    p = argparse.ArgumentParser(prog="twitter delete")
    p.add_argument("tweet", help="Tweet ID or URL")
    args = p.parse_args(argv)

    tweet_id = require_tweet_ref(args.tweet)
    graphql_post(
        Q["DeleteTweet"], "DeleteTweet",
        {"tweet_id": tweet_id, "dark_request": False},
    )
    unrecord_agent_tweet(tweet_id)
    print("Deleted.")
