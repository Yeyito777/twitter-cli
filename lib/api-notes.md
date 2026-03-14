# Twitter/X Internal API — Reverse Engineering Notes

## Auth Model

Three pieces needed for authenticated requests:

1. **Bearer Token** (in `Authorization` header)
   - Static, same for all web users: `Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA`
   - This is Twitter's web app client token

2. **CSRF Token (ct0)** — sent BOTH as:
   - Cookie: `ct0=<value>`
   - Header: `x-csrf-token: <value>`

3. **Auth Token** — httpOnly cookie:
   - Cookie: `auth_token=<value>`
   - This is the actual session credential

## Required Headers

```
Authorization: Bearer <bearer_token>
x-csrf-token: <ct0_value>
x-twitter-auth-type: OAuth2Session
x-twitter-active-user: yes
x-twitter-client-language: en
Content-Type: application/json
```

## Required Cookies

```
auth_token=<session_token>; ct0=<csrf_token>; twid=<user_id>
```

## Token Extraction

Tokens live in qutebrowser-mnemo's Chromium cookie store:
```
sqlite3 ~/.runtime/qutebrowser-mnemo/data/webengine/Cookies \
  "SELECT name, value FROM cookies WHERE host_key LIKE '%x.com%' AND name IN ('auth_token', 'ct0', 'twid')"
```

## Endpoint Patterns

Base: `https://x.com/i/api/graphql/<query_hash>/<OperationName>`

### Known Endpoints (captured 2026-03-01)

| Operation | Hash | Method |
|---|---|---|
| HomeTimeline | `_J734qKR-wyeEf6vCZ1mfQ` | GET |
| DataSaverMode | `xF6sXnKJfS2AOylzxRjf6A` | GET |
| PinnedTimelines | `HaJt3PXnvM-jRdih6zRSxw` | GET |
| getAltTextPromptPreference | `PFIxTk8owMoZgiMccP0r4g` | GET |
| XChatDmSettingsQuery | `zzeLdGlB0ZN6hiOYUIpDcQ` | GET |
| useDirectCallSetupQuery | `zCYojd6h_gVXYjFlaAk4bA` | GET |

### REST Endpoints

| Endpoint | Method |
|---|---|
| `/i/api/1.1/account/settings.json` | GET |
| `/i/api/1.1/hashflags.json` | GET |
| `/i/api/2/badge_count/badge_count.json` | GET |

## GraphQL Request Structure

Endpoints take `variables` and `features` as URL-encoded JSON query params (for GET)
or JSON body (for POST).

### HomeTimeline variables
```json
{"count": 20, "includePromotedContent": true, "requestContext": "launch", "withCommunity": true}
```

### HomeTimeline features (massive blob)
See captured request for full features object — ~30+ feature flags.
