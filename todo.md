# Twitter Terminal Tool — TODO

## Project Structure

```
twitter/
├── bin/twitter      # main command (symlinked to ~/.local/bin/twitter)
├── lib/
│   ├── __init__.py
│   ├── api.py       # authenticated requests + transaction ID generation
│   ├── auth.py      # token extraction from qutebrowser cookie store
│   ├── format.py    # human-readable output formatting
│   ├── parse.py     # response structure → flat tweet dicts
│   ├── api-notes.md # auth model documentation
│   └── endpoints.txt # 161 GraphQL query hashes from client-web bundle
├── downloads/       # media downloads land here
└── todo.md
```

## Phase 0 — Recon & Auth Discovery ✅
- [x] Open x.com in qutebrowser-mnemo, log in
- [x] Capture network traffic with network.sh while browsing
- [x] Identify auth model: bearer token, csrf token (ct0), cookies
- [x] Identify core GraphQL endpoints and their query hashes
- [x] Document findings in lib/api-notes.md
- [x] Validate: curl request to HomeTimeline returns 200 + real data
- [x] Fix: qutebrowser IPC socket was hardcoded in tab scripts → now dynamic

## Phase 1 — Foundation ✅
- [x] Auth extraction module (lib/auth.py) — pulls tokens from qutebrowser SQLite cookie store
- [x] API request module (lib/api.py) — graphql_get, graphql_post, rest_get, rest_post
- [x] x-client-transaction-id generation (required for search + some endpoints)
- [x] Tweet parser (lib/parse.py) — handles Tweet, TweetWithVisibilityResults, retweets, quotes
- [x] Output formatter (lib/format.py) — human-readable with engagement stats, media, threads
- [x] First working command: `twitter timeline`
- [x] --json flag for raw output
- [x] Symlinked to ~/.local/bin/twitter — works from anywhere

## Phase 2 — Core Commands ✅
- [x] `twitter timeline` / `twitter tl` — home feed (paginated, --latest flag)
- [x] `twitter search <query>` / `twitter s` — search tweets (--latest, --count, --cursor)
- [x] `twitter tweet <id_or_url>` — view single tweet
- [x] `twitter profile <user>` / `twitter p` — view user profile
- [x] `twitter post <text>` — post a tweet (--reply, --quote flags)
- [x] `twitter delete <tweet>` — delete own tweet
- [x] `twitter like <tweet>` / `twitter unlike <tweet>`
- [x] `twitter rt <tweet>` / `twitter unrt <tweet>`
- [x] `twitter bookmark <tweet>` / `twitter unbookmark <tweet>`

## Phase 3 — Extended Reading ✅
- [x] `twitter tweets <user>` — view user's tweets (with --replies flag)
- [x] `twitter thread <id_or_url>` — view full thread/conversation
- [x] `twitter notifications` / `twitter notif` — mentions, likes, RTs, follows
- [x] `twitter bookmarks` / `twitter bms` — list saved tweets
- [x] `twitter trending` — trending topics (via ExplorePage + GenericTimelineById)
- [x] `twitter dms` — list DM conversations with last message preview
- [x] `twitter dm <user_or_id>` — read conversation (by @username or conversation ID)
- [x] `twitter dm <user> --send "text"` — send a DM

## Phase 4 — Extended Engagement ✅
- [x] `twitter follow <user>` / `twitter unfollow <user>` — via REST v1.1 friendships
- [x] `twitter mute <user>` / `twitter unmute <user>` — via REST v1.1 mutes
- [x] `twitter block <user>` / `twitter unblock <user>` — via REST v1.1 blocks

## Future Ideas (not planned)
- `twitter post --media <file> <text>` — post with image/video
- `twitter thread-post <text1> --- <text2> --- ...` — post a multi-tweet thread
- `twitter followers [user]` — list followers
- `twitter following [user]` — list following
- Token refresh mechanism (re-extract from browser when expired)
- Better error handling (rate limits, expired tokens, network errors)
- Media download support (save images/videos to downloads/)

## Design Notes

- **Auth approach:** Tokens extracted directly from qutebrowser-mnemo's SQLite
  cookie store. No API keys needed. x-client-transaction-id generated via the
  XClientTransaction library (reverse-engineered from Twitter's JS bundle).
- **From Twitter's perspective:** Requests are indistinguishable from the web client.
  Same bearer token, same headers, same GraphQL endpoints.
- **Tweet references:** Accept both full x.com URLs and raw tweet IDs everywhere.
- **Output:** Default is clean human-readable text. `--json` flag returns parsed data.
- **Query hashes:** Extracted from client-web main bundle. May change on Twitter deploys —
  re-extract by downloading the bundle and grepping for operationName patterns.
