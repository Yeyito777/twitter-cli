"""Twitter/X internal API client — mirrors browser requests."""

import json
import urllib.request
import urllib.parse
import urllib.error
from lib.auth import get_tokens

API_BASE = "https://x.com/i/api"
API_BASE_V2 = "https://api.x.com"

# Transaction ID generator — lazy-initialized, cached per process
_ct = None


def _get_transaction_generator():
    """Initialize the ClientTransaction generator (fetches x.com homepage + ondemand JS).

    This is expensive (~2 requests) so we cache it for the process lifetime.
    """
    global _ct
    if _ct is not None:
        return _ct

    import requests as req_lib
    import bs4
    from x_client_transaction import ClientTransaction
    from x_client_transaction.utils import generate_headers, get_ondemand_file_url

    session = req_lib.Session()
    session.headers = generate_headers()
    home_page = session.get("https://x.com")
    soup = bs4.BeautifulSoup(home_page.content, "html.parser")
    ondemand_url = get_ondemand_file_url(response=soup)
    ondemand_file = session.get(url=ondemand_url)
    _ct = ClientTransaction(
        home_page_response=soup,
        ondemand_file_response=ondemand_file.text,
    )
    return _ct


def _generate_txid(method, path):
    """Generate a valid x-client-transaction-id for a given method + path."""
    ct = _get_transaction_generator()
    return ct.generate_transaction_id(method=method, path=path)

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

# Cache tokens for the lifetime of the process
_tokens = None


def _get_tokens():
    global _tokens
    if _tokens is None:
        _tokens = get_tokens()
    return _tokens


def _build_headers(tokens, method="GET", path=None):
    headers = {
        "Authorization": f"Bearer {tokens['bearer']}",
        "x-csrf-token": tokens["ct0"],
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/134.0.0.0 Safari/537.36"
        ),
    }
    if path:
        headers["x-client-transaction-id"] = _generate_txid(method, path)
    return headers


def _build_cookie_header(tokens):
    return f"auth_token={tokens['auth_token']}; ct0={tokens['ct0']}"


def graphql_get(query_hash, operation_name, variables, features=None):
    """Make an authenticated GET request to a Twitter GraphQL endpoint.

    Returns parsed JSON response.
    """
    tokens = _get_tokens()

    params = {
        "variables": json.dumps(variables, separators=(",", ":")),
        "features": json.dumps(
            features or DEFAULT_FEATURES, separators=(",", ":")
        ),
    }
    path = f"/i/api/graphql/{query_hash}/{operation_name}"
    url = f"https://x.com{path}?{urllib.parse.urlencode(params)}"

    headers = _build_headers(tokens, method="GET", path=path)
    headers["Cookie"] = _build_cookie_header(tokens)

    req = urllib.request.Request(url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body[:500]}")


def graphql_post(query_hash, operation_name, variables, features=None):
    """Make an authenticated POST request to a Twitter GraphQL endpoint.

    Returns parsed JSON response.
    """
    tokens = _get_tokens()

    body = json.dumps({
        "variables": variables,
        "features": features or DEFAULT_FEATURES,
        "queryId": query_hash,
    }, separators=(",", ":")).encode("utf-8")

    path = f"/i/api/graphql/{query_hash}/{operation_name}"
    url = f"https://x.com{path}"

    headers = _build_headers(tokens, method="POST", path=path)
    headers["Cookie"] = _build_cookie_header(tokens)

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body[:500]}")


def rest_get(endpoint, params=None):
    """Make an authenticated GET request to a Twitter REST endpoint.

    endpoint should start with / (e.g. /1.1/account/settings.json)
    Returns parsed JSON response.
    """
    tokens = _get_tokens()

    path = f"/i/api{endpoint}"
    url = f"https://x.com{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    headers = _build_headers(tokens, method="GET", path=path)
    headers["Cookie"] = _build_cookie_header(tokens)

    req = urllib.request.Request(url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body[:500]}")


def rest_post(endpoint, body=None, form=False):
    """Make an authenticated POST request to a Twitter REST endpoint.

    If form=True, sends body as x-www-form-urlencoded instead of JSON.
    This is required for legacy v1.1 endpoints like friendships/create.
    """
    tokens = _get_tokens()

    path = f"/i/api{endpoint}"
    url = f"https://x.com{path}"

    headers = _build_headers(tokens, method="POST", path=path)
    headers["Cookie"] = _build_cookie_header(tokens)

    if form and body:
        data = urllib.parse.urlencode(body).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    else:
        data = None

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        resp_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {resp_body[:500]}")
