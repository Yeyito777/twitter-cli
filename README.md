# twitter-cli

A terminal client for X/Twitter that works by mirroring the requests the web
client makes. From Twitter's servers' perspective, this is indistinguishable
from a normal browser session.

No API keys. No paid tiers. Just your existing browser session.

## How it works

Twitter's web app (x.com) is a JavaScript client that talks to internal GraphQL
and REST endpoints. This tool makes the same HTTP requests, using session
cookies you paste from your browser. That's it — no OAuth apps, no developer
accounts, no API rate limit tiers.

## Quick start

### 1. Install

```bash
git clone https://github.com/Yeyito777/twitter-cli.git
cd twitter-cli

# Install the Python dependency
pip install XClientTransaction

# Symlink to your PATH
ln -sf "$(pwd)/bin/twitter" ~/.local/bin/twitter
```

### 2. Authenticate

```bash
twitter setup
```

This will ask you to paste two cookies from your browser:

1. Open [x.com](https://x.com) and log in
2. Open DevTools (`F12`) → **Application** tab → **Cookies** → `https://x.com`
3. Copy the values for **`auth_token`** and **`ct0`**
4. Paste them when prompted

```
$ twitter setup

  twitter-cli setup
  ─────────────────

  This tool needs two cookies from a logged-in x.com session:
  auth_token and ct0.

  To get them:

  1. Open https://x.com in your browser and log in
  2. Open DevTools (F12) → Application tab → Cookies → https://x.com
  3. Find and copy the values for 'auth_token' and 'ct0'

  Paste auth_token: ****
  Paste ct0: ****

  ✓ Credentials saved to ~/.config/twitter-cli/auth.json
  You're all set! Try 'twitter tl' to see your timeline.
```

Credentials are stored in `~/.config/twitter-cli/auth.json` with `600`
permissions. These are session cookies — they expire when you log out or after
extended inactivity. Just re-run `twitter setup` if that happens.

### 3. Use it

```bash
twitter tl                # Home timeline
twitter search "Claude"   # Search
twitter profile @elonmusk # View a profile
twitter post "Hello"      # Post a tweet
```

## Commands

### Reading

```bash
# Home timeline
twitter timeline                  # or: twitter tl
twitter tl --latest               # Chronological instead of algorithmic
twitter tl -n 5                   # Limit to 5 tweets
twitter tl -c "DAABCgAB..."      # Paginate with cursor

# Single tweet
twitter tweet https://x.com/user/status/1234567890
twitter tweet 1234567890

# Search
twitter search "query"
twitter s "from:elonmusk AI" --latest -n 10

# User profile
twitter profile @user
twitter p elonmusk

# User's tweets
twitter tweets @user
twitter tweets @user --replies    # Include replies

# Thread / conversation
twitter thread 1234567890

# Notifications
twitter notifications             # or: twitter notif

# Bookmarks
twitter bookmarks                 # or: twitter bms

# Trending
twitter trending
```

### Posting

```bash
# Post a tweet
twitter post "Hello from the terminal"

# Reply
twitter reply 1234567890 "Great point"
twitter post "Great point" --reply 1234567890

# Quote tweet
twitter post "Interesting" --quote 1234567890

# Delete
twitter delete 1234567890
```

### Engagement

```bash
twitter like 1234567890
twitter unlike 1234567890
twitter rt 1234567890
twitter unrt 1234567890
twitter bookmark 1234567890
twitter unbookmark 1234567890
```

### Social

```bash
twitter follow @user
twitter unfollow @user
twitter mute @user
twitter unmute @user
twitter block @user
twitter unblock @user
```

### Direct messages

```bash
twitter dms                           # List conversations
twitter dm @user                      # Read conversation
twitter dm @user --send "Hey there"   # Send a DM
```

### Flags

```bash
# JSON output (works on any command)
twitter --json tl -n 5
twitter --json search "query"

# Help
twitter help
twitter help search
```

## Tweet references

Anywhere a tweet is expected, you can pass:
- A full URL: `https://x.com/user/status/1234567890`
- A bare ID: `1234567890`

## Requirements

- Python 3.10+
- [`XClientTransaction`](https://pypi.org/project/XClientTransaction/) (`pip install XClientTransaction`)

The `XClientTransaction` package pulls in `requests` and `beautifulsoup4` as
transitive dependencies.

## How auth works

Twitter's web app uses a static bearer token (the same for every user) plus
two session cookies (`auth_token` and `ct0`) for authentication. Some endpoints
also require a signed `x-client-transaction-id` header, which this tool
generates using the `XClientTransaction` library (it fetches x.com's homepage
JS to compute valid tokens).

All of this is documented in `lib/api-notes.md`.

## Refreshing query hashes

Twitter's GraphQL endpoints use query hashes that change when Twitter deploys
new code. If commands start returning 404 ("Query not found"), the hashes need
to be re-extracted from Twitter's JavaScript bundle:

```bash
# Download the main JS bundle from a loaded x.com page
curl -s "https://abs.twimg.com/responsive-web/client-web/main.<hash>.js" | \
  grep -oP '"[a-zA-Z0-9_/-]{15,40}",operationName:"[A-Z][a-zA-Z]+"' | \
  sed 's/",operationName:"/\t/' | sed 's/"$//g' | sed 's/^"//' | \
  sort -t$'\t' -k2 > lib/endpoints.txt
```

Then update the `Q` dict in `bin/twitter` with the new hashes.

## Configuration

| Item | Default | Override |
|---|---|---|
| Auth file | `~/.config/twitter-cli/auth.json` | `TWITTER_AUTH_FILE` env var |

## Troubleshooting

| Problem | Fix |
|---|---|
| `Not authenticated` | Run `twitter setup` |
| `Auth file is missing: ...` | Run `twitter setup` again |
| `HTTP 404: {"message":"Query not found"}` | Query hashes changed — re-extract (see above) |
| `HTTP 403` | Session expired — log into x.com in your browser, then `twitter setup` |
| `HTTP 429` | Rate limited — wait a few minutes |

## License

[MIT](LICENSE)
