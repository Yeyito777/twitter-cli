"""Shared fakes for tests that exercise the request layer."""

import base64
import json

from src import transport

SNAPSHOT = {
    "ok": True,
    "fetched_at": 0,
    "client": "main.test.js",
    "key": base64.b64encode(bytes(range(48))).decode(),
    "animation_key": "0a0b0c",
    "operations": {
        name: {"queryId": f"live-{name}", "type": "mutation", "features": ["f_on", "f_off"], "fieldToggles": []}
        for name in ("CreateTweet", "DeleteTweet", "FavoriteTweet", "CreateRetweet", "TweetDetail")
    },
    "switches": {"f_on": True, "f_off": False},
}


def response(payload, status=200):
    return transport.Response(status, json.dumps(payload).encode(), "https://x.com/i/api")
