"""The one browser identity every request to X presents.

The cookies come from vimbrowser (CEF Chromium 147 on Linux), so every request
claims exactly that browser: one User-Agent, matching client hints, the
headers Chrome sends for a same-origin fetch from x.com, and Chrome's TLS and
HTTP/2 behaviour (curl_cffi impersonation, see ``transport.py``).  Mixing
identities inside one cookie session (the old Mac Chrome 134 API calls next to
Windows Chrome 127 page loads) is a session-hijack/automation signal.

Values were read from vimbrowser on 2026-10-19 (``navigator.userAgent`` and
``navigator.userAgentData``).  When vimbrowser's Chromium is upgraded, update
CHROME_MAJOR and BRAND; ``twitter doctor`` reports a mismatch.
"""

import re

CHROME_MAJOR = 147
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36"
)
# navigator.userAgentData.brands in vimbrowser (CEF reports Chromium only).
BRANDS = (("Chromium", str(CHROME_MAJOR)), ("Not.A/Brand", "8"))
SEC_CH_UA = ", ".join(f'"{brand}";v="{version}"' for brand, version in BRANDS)
SEC_CH_UA_PLATFORM = '"Linux"'
SEC_CH_UA_MOBILE = "?0"
ACCEPT_LANGUAGE = "en-US,en;q=0.9"
ACCEPT_ENCODING = "gzip, deflate, br, zstd"
# curl_cffi's closest Chrome TLS/HTTP2 profile to Chromium 147.
IMPERSONATE = "chrome146"

ORIGIN = "https://x.com"
DEFAULT_REFERER = "https://x.com/home"
# The page a real session would be on when it makes each request.
REFERERS = {
    "NotificationsTimeline": "https://x.com/notifications",
    "Bookmarks": "https://x.com/i/bookmarks",
    "SearchTimeline": "https://x.com/search",
    "ExplorePage": "https://x.com/explore",
    "GenericTimelineById": "https://x.com/explore",
}


def api_headers(tokens, *, method, txid=None, referer=None, content_type="application/json"):
    """Headers for an x.com/i/api request, in the order Chrome sends them."""
    headers = [
        ("sec-ch-ua-platform", SEC_CH_UA_PLATFORM),
        ("authorization", f"Bearer {tokens['bearer']}"),
        ("x-csrf-token", tokens["ct0"]),
        ("sec-ch-ua", SEC_CH_UA),
        ("x-twitter-client-language", "en"),
        ("sec-ch-ua-mobile", SEC_CH_UA_MOBILE),
        ("x-twitter-active-user", "yes"),
    ]
    if txid:
        headers.append(("x-client-transaction-id", txid))
    headers += [
        ("user-agent", USER_AGENT),
        ("content-type", content_type),
        ("x-twitter-auth-type", "OAuth2Session"),
        ("accept", "*/*"),
    ]
    if method.upper() != "GET":
        headers.append(("origin", ORIGIN))  # same-origin GETs carry no Origin
    headers += [
        ("sec-fetch-site", "same-origin"),
        ("sec-fetch-mode", "cors"),
        ("sec-fetch-dest", "empty"),
        ("referer", referer or DEFAULT_REFERER),
        ("accept-encoding", ACCEPT_ENCODING),
        ("accept-language", ACCEPT_LANGUAGE),
        ("cookie", cookie_header(tokens)),
        ("priority", "u=1, i"),
    ]
    return headers


def document_headers(*, cookie=None):
    """Headers for a top-level page load (what a new tab on x.com sends)."""
    headers = [
        ("sec-ch-ua", SEC_CH_UA),
        ("sec-ch-ua-mobile", SEC_CH_UA_MOBILE),
        ("sec-ch-ua-platform", SEC_CH_UA_PLATFORM),
        ("upgrade-insecure-requests", "1"),
        ("user-agent", USER_AGENT),
        ("accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                   "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"),
        ("sec-fetch-site", "none"),
        ("sec-fetch-mode", "navigate"),
        ("sec-fetch-user", "?1"),
        ("sec-fetch-dest", "document"),
        ("accept-encoding", ACCEPT_ENCODING),
        ("accept-language", ACCEPT_LANGUAGE),
    ]
    if cookie:
        headers.append(("cookie", cookie))
    headers.append(("priority", "u=0, i"))
    return headers


def script_headers():
    """Headers for a script x.com loads from abs.twimg.com."""
    return [
        ("sec-ch-ua-platform", SEC_CH_UA_PLATFORM),
        ("user-agent", USER_AGENT),
        ("sec-ch-ua", SEC_CH_UA),
        ("sec-ch-ua-mobile", SEC_CH_UA_MOBILE),
        ("accept", "*/*"),
        ("origin", ORIGIN),
        ("sec-fetch-site", "cross-site"),
        ("sec-fetch-mode", "cors"),
        ("sec-fetch-dest", "script"),
        ("referer", "https://x.com/"),
        ("accept-encoding", ACCEPT_ENCODING),
        ("accept-language", ACCEPT_LANGUAGE),
    ]


def cookie_header(tokens):
    return f"auth_token={tokens['auth_token']}; ct0={tokens['ct0']}"


def identity_problems():
    """Inconsistencies inside the declared identity (empty when coherent)."""
    problems = []
    ua_major = re.search(r"Chrome/(\d+)\.", USER_AGENT)
    if not ua_major or int(ua_major.group(1)) != CHROME_MAJOR:
        problems.append("User-Agent Chrome version does not match CHROME_MAJOR")
    if dict(BRANDS).get("Chromium") != str(CHROME_MAJOR):
        problems.append("client-hint brands do not match the User-Agent version")
    if "Linux" not in USER_AGENT or SEC_CH_UA_PLATFORM != '"Linux"':
        problems.append("User-Agent and sec-ch-ua-platform disagree on the OS")
    impersonated = re.fullmatch(r"chrome(\d+)", IMPERSONATE)
    if not impersonated or not 0 <= CHROME_MAJOR - int(impersonated.group(1)) <= 2:
        problems.append(f"TLS profile {IMPERSONATE} is not within two versions of Chrome {CHROME_MAJOR}")
    return problems
