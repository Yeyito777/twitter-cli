"""A snapshot of the live X web client, as a page load of x.com/home gives it.

X's web client signs every API call with an ``x-client-transaction-id``
derived from the page it was loaded from (the ``twitter-site-verification``
key and the loading-animation frames) and from key-byte indices in one of its
scripts.  It also sends each GraphQL operation under the query ID of the
deployed bundle.  This module loads ``x.com/home`` the way a browser tab does,
reads those values, and caches them per account.

Two client layouts exist; the snapshot's ``layout`` says which one was served:

- ``client-web`` (until Nov 2026): webpack ``responsive-web/client-web``
  bundles, GraphQL on ``x.com/i/api/graphql`` with feature switches, the
  indices in ``ondemand.s``.
- ``x-web`` (from Nov 18 2026): a Vite + Relay app.  The page loads
  ``x-web/x-web/entry-client-<hash>.js``; operations are Relay persisted
  queries (``params:{id,name,operationKind}``) sent to
  ``api.x.com/graphql/<id>/<name>`` with camelCase variables and no feature
  switches; the indices live in a small module the page loads directly.  The
  tool's reads keep the legacy endpoints (which X still serves); only the
  writes in ``XWEB_MUTATIONS`` / ``XWEB_REST_WRITES`` are sent the x-web way,
  and every other write stays paused on this layout.

Snapshots are cached:

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
XWEB_BASE = "https://abs.twimg.com/x-web/x-web/"
XWEB_ASSETS = XWEB_BASE + "assets/"
XWEB_GRAPHQL = "https://api.x.com/graphql"
XWEB_API_ORIGIN = "https://api.x.com"
# Shared chunks fetched at most while looking for the write operations; a
# /home load in Chrome fetches ~45 scripts.
XWEB_MAX_CHUNKS = 60
# Tool write -> (x-web operation, {x-web variable: tool variable}, response field).
XWEB_MUTATIONS = {
    "FavoriteTweet": ("FavoriteTweet", {"tweetId": "tweet_id"}, "favorite_tweet"),
    "UnfavoriteTweet": ("likeButtonControlUnfavoriteMutation", {"tweetId": "tweet_id"}, "unfavorite_tweet"),
    "CreateRetweet": ("CreateRetweet", {"tweetId": "tweet_id"}, "create_retweet"),
    "DeleteRetweet": ("retweetButtonUnretweetMutation", {"tweetId": "source_tweet_id"}, "unretweet"),
    "CreateBookmark": ("BookmarkAdd", {"tweetId": "tweet_id"}, "tweet_bookmark_put"),
    "DeleteBookmark": ("BookmarkDelete", {"tweetId": "tweet_id"}, "tweet_bookmark_delete"),
}
# REST writes the x-web client sends to api.x.com (form body user_id=...).
XWEB_REST_WRITES = (
    "/1.1/friendships/create.json", "/1.1/friendships/destroy.json",
    "/1.1/mutes/users/create.json", "/1.1/mutes/users/destroy.json",
    "/1.1/blocks/create.json", "/1.1/blocks/destroy.json",
)
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


def _asset(name, base=ASSET_BASE):
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
    response = transport.request("GET", base + name, headers=fingerprint.script_headers())
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


def _load_page(tokens):
    page = transport.request(
        "GET", HOME_URL,
        headers=fingerprint.document_headers(cookie=fingerprint.cookie_header(tokens)),
    )
    if page.status != 200:
        raise WebClientError(f"HTTP {page.status} loading x.com/home")
    if "/login" in page.url or "/i/flow" in page.url or "/onboarding" in page.url:
        raise WebClientError(f"x.com/home redirected to {page.url}: the login is not accepted")
    return page.text()


def _transaction_inputs(html, script):
    """(key, animation_key) of the page, with the indices read from ``script``."""
    import bs4
    from x_client_transaction import ClientTransaction

    soup = bs4.BeautifulSoup(html, "html.parser")
    try:
        client = ClientTransaction(home_page_response=soup, ondemand_file_response=script)
    except Exception as e:
        raise WebClientError(f"transaction-ID inputs not found: {e}") from e
    return client.key, client.animation_key


def fetch(tokens):
    """Load x.com/home like a browser tab and extract what the client needs."""
    html = _load_page(tokens)
    if _XWEB_ENTRY_RE.search(html):
        return _fetch_xweb(html)
    return _fetch_client_web(html)


def _fetch_client_web(html):
    main = re.search(r"https://abs\.twimg\.com/responsive-web/client-web/(main\.[0-9a-f]+\.js)", html)
    if not main:
        raise WebClientError("the page loads neither client-web main.js nor x-web entry-client.js")
    key, animation_key = _transaction_inputs(html, _asset(_chunk_file(html, "ondemand.s")))
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
        "layout": "client-web",
        "client": main.group(1),
        "key": key,
        "animation_key": animation_key,
        "operations": operations,
        "switches": {name: values[name] for name in sorted(used) if name in values},
    }


# ─── The x-web client (Vite + Relay) ───────────────────────

_XWEB_ENTRY_RE = re.compile(r"https://abs\.twimg\.com/x-web/x-web/(entry-client-[\w-]+\.js)")
# The page's inline loaders: ["a","b"].forEach(function(name){var href="…/assets/"+name+".js"…
_XWEB_LOADER_RE = re.compile(
    r'\[((?:"[\w-]+",?)+)\]\.forEach\(function\(name\)\{var href='
    r'"https://abs\.twimg\.com/x-web/x-web/assets/"\+name\+"\.js"'
)
_STATIC_IMPORT_RE = re.compile(r'(?:\bfrom\s*|\bimport\s*)"\./(?:assets/)?([\w-]+\.js)"')
_MAPDEPS_TABLE_RE = re.compile(r'm\.f\|\|\(m\.f=\[(.*?)\]\)')
_MAPDEPS_CALL_RE = re.compile(r'__vite__mapDeps\(\[([\d,]+)\]\)')
_RELAY_OPERATION_RE = re.compile(
    r'params:\{id:`([\w-]+)`,metadata:\{[^{}]*\},name:`(\w+)`,operationKind:`(\w+)`'
)
_TXID_INDEX_RE = re.compile(r"\(\w{1}\[(\d{1,2})\],\s*16\)")


def parse_relay_operations(script):
    """Relay persisted operations: name -> {queryId, type} (no feature switches)."""
    return {
        name: {"queryId": query_id, "type": kind, "features": [], "fieldToggles": []}
        for query_id, name, kind in _RELAY_OPERATION_RE.findall(script)
    }


def parse_initial_switches(html):
    """Feature switches the x-web page boots with (window.__INITIAL_DATA__)."""
    marker = re.search(r"window\.__INITIAL_DATA__\s*=\s*", html)
    if not marker:
        raise WebClientError("the page has no __INITIAL_DATA__")
    data, _end = json.JSONDecoder().raw_decode(html, marker.end())
    features = ((data.get("featureSwitchPayload") or {}).get("features")) or {}
    if not isinstance(features, dict) or not features:
        raise WebClientError("the page has no feature-switch payload")
    return features


def _count_dependencies(text, counts):
    """Score the lazily loaded chunks a script's Vite preload table lists.

    Each lazy import adds 1/len(table) to the chunks it preloads, so chunks
    shared by many nearby features rank before the router's long tail.
    """
    table = _MAPDEPS_TABLE_RE.search(text)
    if not table:
        return
    names = re.findall(r'"assets/([\w-]+\.js)"', table.group(1))
    for call in _MAPDEPS_CALL_RE.findall(text):
        for index in call.split(","):
            if int(index) < len(names):
                counts[names[int(index)]] = counts.get(names[int(index)], 0) + 1 / len(names)


def _fetch_xweb(html):
    entry = _XWEB_ENTRY_RE.search(html).group(1)
    loaded = {}

    def load_chunk(name, base=XWEB_ASSETS):
        if name not in loaded:
            loaded[name] = _asset(name, base)
        return loaded[name]

    # The entry and its static imports: what every x-web page runs.
    pending = [entry]
    while pending:
        name = pending.pop()
        if name in loaded:
            continue
        text = load_chunk(name, XWEB_BASE if name == entry else XWEB_ASSETS)
        pending += [dep for dep in _STATIC_IMPORT_RE.findall(text) if dep not in loaded]

    # The transaction-ID module is one of the scripts the page itself loads.
    page_scripts = sorted({name + ".js" for group in _XWEB_LOADER_RE.findall(html)
                           for name in re.findall(r'"([\w-]+)"', group)})
    txid_modules = [name for name in page_scripts if len(_TXID_INDEX_RE.findall(load_chunk(name))) >= 2]
    if len(txid_modules) != 1:
        raise WebClientError(
            f"expected one transaction-ID module among the page's scripts, found {len(txid_modules)}")
    key, animation_key = _transaction_inputs(html, loaded[txid_modules[0]])

    operations, rest, counts = {}, set(), {}

    def scan(text):
        operations.update(parse_relay_operations(text))
        rest.update(path for path in XWEB_REST_WRITES if path in text)
        _count_dependencies(text, counts)

    for text in loaded.values():
        scan(text)
    # Then the lazily loaded chunks, most widely shared first, until every
    # write this tool can send is found.
    wanted = {op for op, _variables, _field in XWEB_MUTATIONS.values()}
    for _ in range(XWEB_MAX_CHUNKS):
        if wanted <= operations.keys() and len(rest) == len(XWEB_REST_WRITES):
            break
        candidates = [name for name in counts if name not in loaded]
        if not candidates:
            break
        name = min(candidates, key=lambda name: (-counts[name], name))
        scan(load_chunk(name))
    if not operations:
        raise WebClientError("no Relay operations found in the x-web scripts")
    if XWEB_GRAPHQL not in "".join(loaded.values()):
        raise WebClientError(f"the x-web scripts no longer send GraphQL to {XWEB_GRAPHQL}")
    return {
        "ok": True,
        "fetched_at": time.time(),
        "layout": "x-web",
        "client": entry,
        "txid_module": txid_modules[0],
        "key": key,
        "animation_key": animation_key,
        "operations": operations,
        "rest_writes": sorted(rest),
        "switches": parse_initial_switches(html),
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


def layout(snapshot):
    return snapshot.get("layout") or "client-web"


def operation(snapshot, name):
    """(queryId, features) of a legacy GraphQL operation in the live client, or None.

    Only client-web snapshots have them: x-web operations take other variables
    (see ``write_request``), so reads fall back to the built-in query IDs.
    """
    if layout(snapshot) != "client-web":
        return None
    op = (snapshot.get("operations") or {}).get(name)
    if not op:
        return None
    switches = snapshot.get("switches") or {}
    features = {feature: bool(switches.get(feature, False)) for feature in op["features"]}
    return op["queryId"], features


def write_problem(snapshot, operation_name):
    """Why the live client cannot carry this write (None when it can)."""
    if layout(snapshot) == "client-web":
        if operation_name.startswith("/") or operation(snapshot, operation_name):
            return None
        return f"{operation_name} is not an operation of the live web client"
    if operation_name.startswith("/"):
        if operation_name not in XWEB_REST_WRITES:
            return f"{operation_name} is not ported to X's new web client yet"
        if operation_name not in (snapshot.get("rest_writes") or ()):
            return f"{operation_name} was not found in the live web client"
        return None
    mapping = XWEB_MUTATIONS.get(operation_name)
    if not mapping:
        return f"{operation_name} is not ported to X's new web client yet"
    if mapping[0] not in (snapshot.get("operations") or {}):
        return f"{operation_name} ({mapping[0]}) is not an operation of the live web client"
    return None


def write_request(snapshot, operation_name, variables):
    """How the live client sends a GraphQL write: dict(url, path, body, field)."""
    if layout(snapshot) == "client-web":
        query_id, features = operation(snapshot, operation_name)
        path = f"/i/api/graphql/{query_id}/{operation_name}"
        body = {"variables": variables, "features": features, "queryId": query_id}
        return {"url": "https://x.com" + path, "path": path, "body": body, "field": None, "site": "same-origin"}
    name, renames, field = XWEB_MUTATIONS[operation_name]
    query_id = snapshot["operations"][name]["queryId"]
    path = f"/graphql/{query_id}/{name}"
    body = {"queryId": query_id, "variables": {new: variables[old] for new, old in renames.items()}}
    return {"url": XWEB_API_ORIGIN + path, "path": path, "body": body, "field": field, "site": "same-site"}


def rest_write_request(snapshot, endpoint):
    """How the live client sends a REST write: dict(url, path, site)."""
    if layout(snapshot) == "client-web":
        path = "/i/api" + endpoint
        return {"url": "https://x.com" + path, "path": path, "site": "same-origin"}
    return {"url": XWEB_API_ORIGIN + endpoint, "path": endpoint, "site": "same-site"}
