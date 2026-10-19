"""HTTP to X with Chrome's TLS and HTTP/2 behaviour.

curl_cffi impersonates Chrome's TLS ClientHello and HTTP/2 settings, so the
transport matches the browser the headers claim to be.  Python's urllib
(OpenSSL, HTTP/1.1) is unmistakably not a browser; it is only used for reads
when curl_cffi is missing, and writes refuse to run without impersonation.
"""

import urllib.error
import urllib.request

from src.fingerprint import IMPERSONATE

TIMEOUT_SECONDS = 30

try:
    from curl_cffi import requests as _curl
except ImportError:  # pragma: no cover - exercised only on a broken install
    _curl = None

_session = None


def impersonation_problem():
    """Why requests would not look like Chrome on the wire, or None."""
    if _curl is None:
        return "curl_cffi is not installed (requests would use Python's TLS stack)"
    try:
        from curl_cffi.requests import BrowserType

        BrowserType(IMPERSONATE)
    except (ImportError, ValueError):
        return f"curl_cffi cannot impersonate {IMPERSONATE}"
    return None


def _get_session():
    global _session
    if _session is None:
        _session = _curl.Session(impersonate=IMPERSONATE, default_headers=False)
    return _session


class Response:
    def __init__(self, status, body, url, cookies=None):
        self.status = status
        self.body = body
        self.url = url
        self.cookies = cookies or {}

    def text(self):
        return self.body.decode("utf-8", errors="replace")


def request(method, url, *, headers, data=None, timeout=TIMEOUT_SECONDS, allow_fallback=True):
    """Send one request; returns a Response for any HTTP status.

    ``headers`` is an ordered list of (name, value) pairs.  Network failures
    raise OSError (the request may or may not have reached X).
    """
    if _curl is not None:
        session = _get_session()
        # Never let one response's Set-Cookie ride along on a later request:
        # the notify daemon serves several accounts from one process, and each
        # request carries exactly the cookies its caller put in the headers.
        session.cookies.clear()
        try:
            response = session.request(
                method, url, headers=dict(headers), data=data, timeout=timeout,
                allow_redirects=True,
            )
        except Exception as e:  # curl_cffi raises its own RequestException types
            raise OSError(f"{type(e).__name__}: {e}") from e
        cookies = {cookie.name: cookie.value for cookie in response.cookies.jar}
        return Response(response.status_code, response.content, str(response.url), cookies)
    if not allow_fallback:
        raise OSError(impersonation_problem())
    # urllib cannot decode br/zstd, so it only offers what it can read.
    plain = [(k, "gzip, deflate" if k.lower() == "accept-encoding" else v) for k, v in headers]
    req = urllib.request.Request(url, data=data, headers=dict(plain), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return Response(resp.status, _decode(resp), resp.url, _set_cookies(resp))
    except urllib.error.HTTPError as e:
        return Response(e.code, _decode(e), url, _set_cookies(e))


def _set_cookies(resp):
    from http.cookies import SimpleCookie

    jar = SimpleCookie()
    for header in resp.headers.get_all("Set-Cookie") or []:
        try:
            jar.load(header)
        except Exception:
            continue
    return {name: morsel.value for name, morsel in jar.items()}


def _decode(resp):
    body = resp.read()
    encoding = (resp.headers.get("Content-Encoding") or "").lower()
    if encoding == "gzip":
        import gzip

        return gzip.decompress(body)
    if encoding == "deflate":
        import zlib

        return zlib.decompress(body)
    return body
