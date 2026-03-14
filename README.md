# twitter — Terminal Client for X/Twitter

A terminal tool that gives full access to Twitter/X by mirroring the requests
that the web client makes. From Twitter's servers' perspective, this is
indistinguishable from a normal browser session.

No API keys. No paid tiers. Just your existing browser session.

## How It Works

Twitter's web app (x.com) is a JavaScript client that talks to internal GraphQL
endpoints. This tool:

1. Extracts auth tokens from your logged-in qutebrowser-mnemo session
2. Generates valid `x-client-transaction-id` headers (required by some endpoints)
3. Makes the same HTTP requests the browser would make
4. Parses the responses into clean, readable output

## Prerequisites

- qutebrowser-mnemo must be running with an active Twitter login at x.com
- Python 3.10+
- `XClientTransaction` library (`pip install XClientTransaction`)
- `beautifulsoup4` and `requests` (dependencies of XClientTransaction)

## Installation

Already installed — symlinked to `~/.local/bin/twitter`.

To reinstall:
```
ln -sf ~/Workspace/twitter/bin/twitter ~/.local/bin/twitter
```

## Usage

### Reading

```bash
# Home timeline (algorithmic)
twitter timeline
twitter tl

# Home timeline (latest / chronological)
twitter tl --latest

# Specific number of tweets
twitter tl -n 5

# Paginate with cursor (shown at bottom of output)
twitter tl -c "DAABCgABGXlN..."

# View a single tweet (by URL or ID)
twitter tweet https://x.com/elonmusk/status/1234567890
twitter tweet 1234567890

# Search
twitter search "Claude AI"
twitter s "machine learning" --latest -n 10

# View a user's profile
twitter profile @AnthropicAI
twitter p elonmusk
```

### Posting

```bash
# Post a tweet
twitter post "Hello from the terminal"

# Reply to a tweet
twitter post "Great point" --reply https://x.com/user/status/123
twitter post "Agreed" -r 123456789

# Quote tweet
twitter post "This is interesting" --quote 123456789
```

### Engagement

```bash
# Like / unlike
twitter like 123456789
twitter unlike https://x.com/user/status/123

# Retweet / undo
twitter rt 123456789
twitter unrt 123456789

# Bookmark / remove
twitter bookmark 123456789
twitter unbookmark 123456789

# Delete your own tweet
twitter delete 123456789
```

### Flags

```bash
# JSON output (on any command)
twitter --json tl -n 5
twitter --json search "query"
twitter --json tweet 123456789

# Help
twitter help
twitter help post
twitter help search
```

## Tweet References

Anywhere a tweet is expected, you can use:
- A full URL: `https://x.com/user/status/1234567890`
- A bare ID: `1234567890`

## Architecture

```
twitter/
├── bin/twitter          # CLI entry point (argparse-based)
├── lib/
│   ├── auth.py          # Token extraction from qutebrowser cookie store
│   ├── api.py           # HTTP requests + transaction ID generation
│   ├── parse.py         # Response JSON → flat tweet dicts
│   ├── format.py        # Tweet dicts → terminal-friendly text
│   ├── endpoints.txt    # All 161 GraphQL query hashes
│   └── api-notes.md     # Reverse engineering notes
├── downloads/           # Media downloads
└── todo.md              # Development roadmap
```

### Auth Flow

```
qutebrowser-mnemo cookie store (SQLite)
    → auth.py extracts auth_token + ct0
    → api.py builds headers identical to browser
    → x-client-transaction-id generated via XClientTransaction lib
    → requests hit x.com/i/api/graphql/... endpoints
    → responses parsed by parse.py
    → formatted by format.py
```

### Key Files

- **auth.py** — Reads `auth_token` and `ct0` cookies from
  `~/.runtime/qutebrowser-mnemo/data/webengine/Cookies` (Chromium SQLite format).
  The bearer token is static (same for all Twitter web users).

- **api.py** — Makes authenticated GET/POST requests to Twitter's GraphQL API.
  Generates `x-client-transaction-id` headers using the XClientTransaction library
  (which fetches x.com's homepage and ondemand JS to compute valid signed tokens).
  The generator is initialized once per process and cached.

- **parse.py** — Navigates Twitter's deeply nested GraphQL response structures.
  Handles `Tweet`, `TweetWithVisibilityResults`, retweets (nested original),
  quote tweets (nested quoted), and timeline cursor entries.

- **format.py** — Renders tweets with engagement stats (♥ 🔁 💬 👁),
  media attachments, quote tweet boxes, reply context, and retweet attribution.

- **endpoints.txt** — 161 GraphQL query hashes extracted from Twitter's
  `client-web/main.*.js` bundle. These hashes change on Twitter deploys.

## Refreshing Query Hashes

If commands start returning 404 ("Query not found"), the query hashes have
changed. To re-extract:

```bash
# Find the current main bundle URL from a loaded Twitter page
TAB=~/.runtime/qutebrowser-mnemo/runtime/tabs/<tab_id>
cat $TAB/network.json | jq -r '.requests[].url' | grep "client-web/main"

# Download and extract all hashes
curl -s "<bundle_url>" | \
  grep -oP '"[a-zA-Z0-9_/-]{15,40}",operationName:"[A-Z][a-zA-Z]+"' | \
  sed 's/",operationName:"/\t/' | sed 's/"$//g' | sed 's/^"//' | \
  sort -t$'\t' -k2 > lib/endpoints.txt
```

Then update the `Q` dict in `bin/twitter` with the new hashes.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Cookie database not found` | Start qutebrowser-mnemo and log into x.com |
| `Missing Twitter cookies: auth_token, ct0` | Log into x.com in qutebrowser-mnemo |
| `HTTP 404: {"message":"Query not found"}` | Query hashes changed — re-extract (see above) |
| `HTTP 403` | Session may have expired — re-login in qutebrowser-mnemo |
| `HTTP 429` | Rate limited — wait a few minutes |
