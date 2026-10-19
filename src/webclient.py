"""A snapshot of the live X web client, as a page load of x.com/home gives it.

X's web client signs every API call with an ``x-client-transaction-id``
derived from the page it was loaded from (the ``twitter-site-verification``
key and the loading-animation frames) and from constants in the
``ondemand.s`` script.  It also sends each GraphQL operation under the query
ID and feature switches of the deployed bundle.  This module loads
``x.com/home`` the way a browser tab does, reads those values, and caches them
per account:

- a working snapshot for SNAPSHOT_TTL (a tab's lifetime), then reloads;
- a failure for FAILURE_TTL, so a broken client is not re-fetched before
  every call (writes stay paused meanwhile, see ``write_guard``);
- the content-hashed scripts on disk forever (they never change).
"""

import base64
import hashlib
import json
import math
import os
import random
import re
import time
from pathlib import Path

from src import fingerprint, transport

SNAPSHOT_TTL = 45 * 60
FAILURE_TTL = 30 * 60
HOME_URL = "https://x.com/home"
ASSET_BASE = "https://abs.twimg.com/responsive-web/client-web/"
# Pages whose bundles (and shared chunks) define GraphQL operations this tool
# uses outside main.js, e.g. HomeTimeline lives in a shared~…~bundle.HomeTimeline chunk.
OPERATION_PAGES = ("HomeTimeline", "Notifications", "Bookmarks")
# Constants of X's transaction-ID scheme (as in XClientTransaction).
TXID_EPOCH = 1682924400
TXID_KEYWORD = "obfiowerehiring"
TXID_EXTRA_BYTE = 3

_memory = {}


class WebClientError(RuntimeError):
    """The live web client could not be loaded or understood."""


# ─── Snapshot cache ────────────────────────────────────────


def _snapshot_file(alias):
    from src.accounts import account_dir

    return account_dir(alias) / "webclient.json"


def _asset_dir():
    root = os.environ.get("XDG_CACHE_HOME", "").strip() or str(Path.home() / ".cache")
    return Path(root) / "twitter-cli" / "assets"


def _read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, separators=(",", ":")))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def load(tokens, alias, *, refresh=False):
    """The account's current web-client snapshot; raises WebClientError."""
    now = time.time()
    path = _snapshot_file(alias)
    cached = None if refresh else (_memory.get(path) or _read_json(path))
    if cached and isinstance(cached.get("fetched_at"), (int, float)):
        age = now - cached["fetched_at"]
        if cached.get("ok") and age < SNAPSHOT_TTL:
            _memory[path] = cached
            return cached
        if not cached.get("ok") and age < FAILURE_TTL:
            _memory[path] = cached
            raise WebClientError(f"{cached.get('error')} (cached; retried after {FAILURE_TTL // 60} min)")
    try:
        snapshot = fetch(tokens)
    except Exception as e:
        failure = {"ok": False, "fetched_at": now, "error": f"{type(e).__name__}: {e}"[:500]}
        _memory[path] = failure
        try:
            _write_json(path, failure)
        except OSError:
            pass
        raise WebClientError(failure["error"]) from e
    _memory[path] = snapshot
    _write_json(path, snapshot)
    return snapshot


# ─── Fetching and parsing ──────────────────────────────────


def _asset(name):
    """A content-hashed client script, from the disk cache or abs.twimg.com."""
    if not re.fullmatch(r"[\w.~-]+\.js", name):
        raise WebClientError(f"unexpected script name {name!r}")
    path = _asset_dir() / name
    try:
        text = path.read_text()
        os.utime(path)  # still in use: keep it past pruning
        return text
    except OSError:
        pass
    response = transport.request("GET", ASSET_BASE + name, headers=fingerprint.script_headers())
    if response.status != 200:
        raise WebClientError(f"HTTP {response.status} loading {name}")
    text = response.text()
    path.parent.mkdir(parents=True, exist_ok=True)
    _prune_assets(path.parent)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(path)
    return text


ASSET_MAX_AGE = 30 * 24 * 60 * 60


def _prune_assets(directory):
    """Drop scripts of old client deployments; each new deploy adds ~2 MB."""
    cutoff = time.time() - ASSET_MAX_AGE
    for old in directory.glob("*.js"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass


def _operation_chunks(html):
    names = re.findall(r'[,{]\d+:"((?:shared~)?bundle\.[\w.~-]+)"', html)
    wanted = {f"bundle.{page}" for page in OPERATION_PAGES}
    return sorted({name for name in names if wanted & set(name.split("~"))})


def _chunk_file(html, chunk_name):
    """Map a webpack chunk name to its file via the page's chunk tables."""
    index = re.search(r'[,{](\d+):"' + re.escape(chunk_name) + '"', html)
    if not index:
        raise WebClientError(f"the page does not reference {chunk_name}")
    digest = re.search(r'[,{]' + index.group(1) + r':"([0-9a-f]+)"', html)
    if not digest:
        raise WebClientError(f"no file hash for {chunk_name}")
    return f"{chunk_name}.{digest.group(1)}a.js"


_OPERATION_RE = re.compile(
    r'queryId:"([\w-]+)",operationName:"(\w+)",operationType:"(\w+)"'
    r'(?:,metadata:\{featureSwitches:\[([^\]]*)\](?:,fieldToggles:\[([^\]]*)\])?)?'
)


def parse_operations(script):
    operations = {}
    for query_id, name, kind, switches, toggles in _OPERATION_RE.findall(script):
        operations[name] = {
            "queryId": query_id,
            "type": kind,
            "features": re.findall(r'"([\w-]+)"', switches or ""),
            "fieldToggles": re.findall(r'"([\w-]+)"', toggles or ""),
        }
    return operations


def parse_feature_switches(html):
    """Feature-switch values the page boots with (defaults, then user overrides)."""
    marker = re.search(r"window\.__INITIAL_STATE__\s*=\s*", html)
    if not marker:
        raise WebClientError("the page has no __INITIAL_STATE__")
    state, _end = json.JSONDecoder().raw_decode(html, marker.end())
    switches = state.get("featureSwitch") or {}
    values = {}
    for config in ((switches.get("defaultConfig") or {}), ((switches.get("user") or {}).get("config") or {})):
        for name, entry in config.items():
            if isinstance(entry, dict) and "value" in entry:
                values[name] = entry["value"]
    if not values:
        raise WebClientError("the page has no feature-switch configuration")
    return values


def fetch(tokens):
    """Load x.com/home like a browser tab and extract what the client needs."""
    import bs4
    from x_client_transaction import ClientTransaction

    page = transport.request(
        "GET", HOME_URL,
        headers=fingerprint.document_headers(cookie=fingerprint.cookie_header(tokens)),
    )
    if page.status != 200:
        raise WebClientError(f"HTTP {page.status} loading x.com/home")
    if "/login" in page.url or "/i/flow" in page.url or "/onboarding" in page.url:
        raise WebClientError(f"x.com/home redirected to {page.url}: the login is not accepted")
    html = page.text()

    main = re.search(r"https://abs\.twimg\.com/responsive-web/client-web/(main\.[0-9a-f]+\.js)", html)
    if not main:
        raise WebClientError("the page does not load client-web main.js")
    ondemand = _asset(_chunk_file(html, "ondemand.s"))
    soup = bs4.BeautifulSoup(html, "html.parser")
    try:
        client = ClientTransaction(home_page_response=soup, ondemand_file_response=ondemand)
    except Exception as e:
        raise WebClientError(f"transaction-ID inputs not found: {e}") from e

    operations = parse_operations(_asset(main.group(1)))
    for chunk in _operation_chunks(html):
        operations.update(parse_operations(_asset(_chunk_file(html, chunk))))
    if not operations:
        raise WebClientError("no GraphQL operations found in the client scripts")
    values = parse_feature_switches(html)
    used = {name for op in operations.values() for name in op["features"]}
    return {
        "ok": True,
        "fetched_at": time.time(),
        "client": main.group(1),
        "key": client.key,
        "animation_key": client.animation_key,
        "operations": operations,
        "switches": {name: values[name] for name in sorted(used) if name in values},
    }


# ─── Using a snapshot ──────────────────────────────────────


def transaction_id(snapshot, method, path, *, now=None, random_byte=None):
    """The x-client-transaction-id the web client sends for ``method path``."""
    time_now = math.floor((time.time() if now is None else now) - TXID_EPOCH)
    time_bytes = [(time_now >> (8 * i)) & 0xFF for i in range(4)]
    key_bytes = list(base64.b64decode(snapshot["key"]))
    digest = hashlib.sha256(
        f"{method}!{path}!{time_now}{TXID_KEYWORD}{snapshot['animation_key']}".encode()
    ).digest()
    salt = random.randint(0, 255) if random_byte is None else random_byte
    payload = [*key_bytes, *time_bytes, *digest[:16], TXID_EXTRA_BYTE]
    return base64.b64encode(bytes([salt, *(b ^ salt for b in payload)])).decode().strip("=")


def operation(snapshot, name):
    """(queryId, features) of a GraphQL operation in the live client, or None."""
    op = (snapshot.get("operations") or {}).get(name)
    if not op:
        return None
    switches = snapshot.get("switches") or {}
    features = {feature: bool(switches.get(feature, False)) for feature in op["features"]}
    return op["queryId"], features
