"""Twitter/X internal API client: requests exactly as the web client sends them.

Every request presents one browser identity (``fingerprint``) over Chrome's
TLS/HTTP2 (``transport``), signed with an x-client-transaction-id and using
the query IDs and feature switches of the live web client (``webclient``).
Writes additionally pass ``write_guard`` before anything is sent.
"""

import json
import urllib.parse

from src import fingerprint, transport, webclient
from src.auth import get_tokens
from src.write_guard import ensure_writes_allowed, record_write_error

API_ORIGIN = "https://x.com"
# Every request has a deadline: a stalled read must not hang a command, or the
# notify daemon's single polling loop, forever.
HTTP_TIMEOUT_SECONDS = transport.TIMEOUT_SECONDS


class TwitterAPIError(RuntimeError):
    """An error response from X, with its HTTP status and X error codes."""

    def __init__(self, status, codes=(), message=""):
        self.status = status
        self.codes = frozenset(int(code) for code in codes)
        super().__init__(f"HTTP {status}: {message}")


def error_codes(payload):
    """X error codes in a response body (GraphQL often returns them with HTTP 200)."""
    errors = payload.get("errors") if isinstance(payload, dict) else None
    codes = []
    for error in errors if isinstance(errors, list) else []:
        code = error.get("code") if isinstance(error, dict) else None
        if isinstance(code, int) or (isinstance(code, str) and code.isdigit()):
            codes.append(int(code))
    return codes


# Feature flags required by most GraphQL endpoints (captured 2026-03-01)
DEFAULT_FEATURES = {
    "rweb_video_screen_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": False,
    "rweb_tipjar_consumption_enabled": False,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "responsive_web_jetfuel_frame": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "responsive_web_grok_annotations_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "content_disclosure_indicator_enabled": True,
    "content_disclosure_ai_generated_indicator_enabled": True,
    "responsive_web_grok_show_grok_translated_post": False,
    "responsive_web_grok_analysis_button_from_backend": True,
    "post_ctas_fetch_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": False,
    "responsive_web_enhance_cards_enabled": False,
}


def _snapshot(tokens):
    """The live web-client snapshot for these tokens' account, or None."""
    if "alias" not in tokens:
        return None  # credentials being verified before they are saved
    try:
        return webclient.load(tokens, tokens["alias"])
    except webclient.WebClientError:
        return None


def _send(method, path, tokens, *, query=None, body=None, content_type="application/json",
          referer=None, snapshot=None, allow_fallback=True):
    """One request to x.com/i/api; returns parsed JSON or raises TwitterAPIError."""
    txid = webclient.transaction_id(snapshot, method, path) if snapshot else None
    url = API_ORIGIN + path + (f"?{urllib.parse.urlencode(query)}" if query else "")
    headers = fingerprint.api_headers(
        tokens, method=method, txid=txid, referer=referer, content_type=content_type,
    )
    response = transport.request(method, url, headers=headers, data=body, allow_fallback=allow_fallback)
    text = response.text()
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        data = None
    if response.status >= 400:
        raise TwitterAPIError(response.status, error_codes(data), text[:500])
    if data is None:
        raise TwitterAPIError(response.status, (), f"non-JSON response: {text[:200]}")
    return data


def _graphql(operation_name, query_hash, features, snapshot):
    live = webclient.operation(snapshot, operation_name) if snapshot else None
    if live:
        return live
    return query_hash, features or DEFAULT_FEATURES


def graphql_get(query_hash, operation_name, variables, features=None, *, tokens=None):
    """Make an authenticated GET request to a Twitter GraphQL endpoint.

    Uses the selected account unless explicit tokens are given.  The live
    client's query ID and features win over the hardcoded fallbacks.
    """
    tokens = tokens or get_tokens()
    snapshot = _snapshot(tokens)
    query_id, features = _graphql(operation_name, query_hash, features, snapshot)
    query = {
        "variables": json.dumps(variables, separators=(",", ":")),
        "features": json.dumps(features, separators=(",", ":")),
    }
    return _send(
        "GET", f"/i/api/graphql/{query_id}/{operation_name}", tokens, query=query,
        referer=fingerprint.REFERERS.get(operation_name), snapshot=snapshot,
    )


def graphql_post(query_hash, operation_name, variables, features=None, *, tokens=None):
    """Make an authenticated GraphQL mutation (a write).

    Returns parsed JSON response.
    """
    tokens = tokens or get_tokens()
    snapshot = ensure_writes_allowed(operation_name, tokens)
    query_id, features = webclient.operation(snapshot, operation_name)
    body = json.dumps({
        "variables": variables,
        "features": features,
        "queryId": query_id,
    }, separators=(",", ":")).encode("utf-8")
    return _send_write(
        "POST", f"/i/api/graphql/{query_id}/{operation_name}", tokens, operation_name,
        body=body, snapshot=snapshot,
    )


def _send_write(method, path, tokens, operation, **kwargs):
    """Send one write; X errors (even with HTTP 200) raise, and may pause writes."""
    try:
        data = _send(method, path, tokens, allow_fallback=False, **kwargs)
    except TwitterAPIError as error:
        record_write_error(error.codes, str(error), operation)
        raise
    except OSError as error:
        raise TwitterAPIError(
            0, (), f"Outcome unknown: the connection failed during {operation} ({error}); "
                   "X may have applied it. Check before retrying."
        ) from error
    if isinstance(data, dict) and data.get("errors"):
        codes = error_codes(data)
        messages = "; ".join(
            str(err.get("message") or err) for err in data["errors"] if isinstance(err, dict)
        ) or json.dumps(data["errors"])[:300]
        record_write_error(codes, messages, operation)
        raise TwitterAPIError(200, codes, messages)
    return data


def rest_get(endpoint, params=None, *, tokens=None):
    """Make an authenticated GET request to a Twitter REST endpoint.

    endpoint should start with / (e.g. /1.1/account/settings.json)
    Returns parsed JSON response.
    """
    tokens = tokens or get_tokens()
    return _send("GET", f"/i/api{endpoint}", tokens, query=params, snapshot=_snapshot(tokens))


def rest_post(endpoint, body=None, form=False, *, tokens=None):
    """Make an authenticated POST request to a Twitter REST endpoint (a write).

    If form=True, sends body as x-www-form-urlencoded instead of JSON.
    This is required for legacy v1.1 endpoints like friendships/create.
    """
    tokens = tokens or get_tokens()
    snapshot = ensure_writes_allowed(endpoint, tokens)
    content_type = "application/json"
    if form and body:
        data = urllib.parse.urlencode(body).encode("utf-8")
        content_type = "application/x-www-form-urlencoded"
    elif body:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    else:
        data = None
    return _send_write(
        "POST", f"/i/api{endpoint}", tokens, endpoint, body=data,
        content_type=content_type, snapshot=snapshot,
    )
