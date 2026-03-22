"""Engagement commands — like, rt, bookmark, follow, mute, block."""

import argparse

from src.api import graphql_post, rest_post
from src.helpers import Q, require_tweet_ref, resolve_user_id


# ─── Tweet actions ────────────────────────────────────────


def like(argv):
    """Like a tweet."""
    p = argparse.ArgumentParser(prog="twitter like")
    p.add_argument("tweet", help="Tweet ID or URL")
    args = p.parse_args(argv)

    tweet_id = require_tweet_ref(args.tweet)
    graphql_post(Q["FavoriteTweet"], "FavoriteTweet", {"tweet_id": tweet_id})
    print("Liked.")


def unlike(argv):
    """Unlike a tweet."""
    p = argparse.ArgumentParser(prog="twitter unlike")
    p.add_argument("tweet", help="Tweet ID or URL")
    args = p.parse_args(argv)

    tweet_id = require_tweet_ref(args.tweet)
    graphql_post(Q["UnfavoriteTweet"], "UnfavoriteTweet", {"tweet_id": tweet_id})
    print("Unliked.")


def rt(argv):
    """Retweet a tweet."""
    p = argparse.ArgumentParser(prog="twitter rt")
    p.add_argument("tweet", help="Tweet ID or URL")
    args = p.parse_args(argv)

    tweet_id = require_tweet_ref(args.tweet)
    graphql_post(
        Q["CreateRetweet"], "CreateRetweet",
        {"tweet_id": tweet_id, "dark_request": False},
    )
    print("Retweeted.")


def unrt(argv):
    """Undo a retweet."""
    p = argparse.ArgumentParser(prog="twitter unrt")
    p.add_argument("tweet", help="Tweet ID or URL")
    args = p.parse_args(argv)

    tweet_id = require_tweet_ref(args.tweet)
    graphql_post(
        Q["DeleteRetweet"], "DeleteRetweet",
        {"source_tweet_id": tweet_id, "dark_request": False},
    )
    print("Unretweeted.")


def bookmark(argv):
    """Bookmark a tweet."""
    p = argparse.ArgumentParser(prog="twitter bookmark")
    p.add_argument("tweet", help="Tweet ID or URL")
    args = p.parse_args(argv)

    tweet_id = require_tweet_ref(args.tweet)
    graphql_post(Q["CreateBookmark"], "CreateBookmark", {"tweet_id": tweet_id})
    print("Bookmarked.")


def unbookmark(argv):
    """Remove a bookmark from a tweet."""
    p = argparse.ArgumentParser(prog="twitter unbookmark")
    p.add_argument("tweet", help="Tweet ID or URL")
    args = p.parse_args(argv)

    tweet_id = require_tweet_ref(args.tweet)
    graphql_post(Q["DeleteBookmark"], "DeleteBookmark", {"tweet_id": tweet_id})
    print("Unbookmarked.")


# ─── User actions ─────────────────────────────────────────


def _user_action(args, endpoint, past_tense):
    """Perform a user-targeted REST action (follow, mute, block, etc.)."""
    username = args.user.lstrip("@")
    user_id, _ = resolve_user_id(username)
    rest_post(endpoint, {"user_id": user_id}, form=True)
    print(f"{past_tense} @{username}.")


def follow(argv):
    """Follow a user."""
    p = argparse.ArgumentParser(prog="twitter follow")
    p.add_argument("user", help="Username (with or without @)")
    args = p.parse_args(argv)
    _user_action(args, "/1.1/friendships/create.json", "Followed")


def unfollow(argv):
    """Unfollow a user."""
    p = argparse.ArgumentParser(prog="twitter unfollow")
    p.add_argument("user", help="Username (with or without @)")
    args = p.parse_args(argv)
    _user_action(args, "/1.1/friendships/destroy.json", "Unfollowed")


def mute(argv):
    """Mute a user."""
    p = argparse.ArgumentParser(prog="twitter mute")
    p.add_argument("user", help="Username (with or without @)")
    args = p.parse_args(argv)
    _user_action(args, "/1.1/mutes/users/create.json", "Muted")


def unmute(argv):
    """Unmute a user."""
    p = argparse.ArgumentParser(prog="twitter unmute")
    p.add_argument("user", help="Username (with or without @)")
    args = p.parse_args(argv)
    _user_action(args, "/1.1/mutes/users/destroy.json", "Unmuted")


def block(argv):
    """Block a user."""
    p = argparse.ArgumentParser(prog="twitter block")
    p.add_argument("user", help="Username (with or without @)")
    args = p.parse_args(argv)
    _user_action(args, "/1.1/blocks/create.json", "Blocked")


def unblock(argv):
    """Unblock a user."""
    p = argparse.ArgumentParser(prog="twitter unblock")
    p.add_argument("user", help="Username (with or without @)")
    args = p.parse_args(argv)
    _user_action(args, "/1.1/blocks/destroy.json", "Unblocked")
