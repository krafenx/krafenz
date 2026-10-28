# Cloudflare Worker setup

This project is deployed as one Cloudflare Worker with static assets. The backend lives in `worker.js`; there is no `functions/api` Pages Functions backend anymore.

## Files to keep in the Worker deploy

- `worker.js`
- `wrangler.jsonc`
- `_headers`
- `.assetsignore`
- `index.html`
- `watchlist.html`
- `gamelist.html`
- `tasks.html`
- `setup.html`
- `wishlist.html`
- image/static assets such as `favicon.png`, `1illustration.png`, `CNAME`, `sitemap.xml`

The old `stream.html`, `test.html`, `wall.html`, and Pages Functions files were removed.

## Required Cloudflare settings

Create or keep one Workers KV namespace and bind it as:

- `WATCHLIST`

One namespace holds every collection; they are separated by key:

| Key | Contents | Endpoint | Page |
| --- | --- | --- | --- |
| `items` | watchlist (anime/movies/series) | `/api/watchlist` | `watchlist.html` |
| `games` | gamelist | `/api/gamelist` | `gamelist.html` |
| `setup` | PC build + other gear | `/api/setup` | `setup.html` |
| `wishlist` | shopping wishlist | `/api/wishlist` | `wishlist.html` |
| `igdb:token`, `twitch:token` | short-lived OAuth tokens (TTL) | internal | - |
| `spotify:refresh_token`, `spotify:access_token` | Spotify OAuth tokens | internal | - |

Add these secrets:

- `ADMIN_PASSWORD`
- `TMDB_ACCESS_TOKEN`
- `LASTFM_API_KEY`
- `SPOTIFY_CLIENT_ID`
- `SPOTIFY_CLIENT_SECRET`
- `TWITCH_CLIENT_ID`
- `TWITCH_CLIENT_SECRET`

The Worker also uses Cloudflare Workers Rate Limiting bindings configured in `wrangler.jsonc`:

- `ADMIN_LOGIN_RATE_LIMIT` - admin login attempts
- `WRITE_RATE_LIMIT` - watchlist/gamelist saves
- `GAME_SEARCH_RATE_LIMIT` - IGDB search
- `PUBLIC_API_RATE_LIMIT` - public search and Twitch status

Use Wrangler 4.36 or newer when deploying the `ratelimits` config:

```bash
npx wrangler deploy
```

## Why KV usage is lower now

The removed stream/test pages used polling and could create thousands of KV reads per open tab. They are gone.

Rate limiting no longer stores counters in KV. It uses Cloudflare Rate Limiting bindings instead, so login/search/write throttling does not spend daily Workers KV operations.

KV is still used for:

- watchlist data
- gamelist data
- setup data (`setup.html`)
- wishlist data (`wishlist.html`)
- cached temporary IGDB/Twitch access tokens (read first; written only when a token is issued or renewed)

`/api/setup` and `/api/wishlist` follow the same caching rules as the other lists: public reads are
served from the Workers Cache API for five minutes and the cache entry is dropped after an admin save,
so a busy page still costs one KV read per five minutes rather than one per visitor.

The homepage checks Twitch at most once every five minutes per open tab. The Worker additionally keeps a five-minute Twitch status cache in the Workers Cache API, so cache hits do not make the two Helix API requests and do not write to KV. If Twitch rejects a cached access token with `401`, the Worker requests and stores one replacement token, then retries the status request once.

The Last.fm API key is kept in the `LASTFM_API_KEY` secret and never sent to browsers. Set the non-secret `LASTFM_USER` variable in `wrangler.jsonc` if the profile changes. The Worker caches recent-track data for five seconds. Spotify credentials are stored as secrets; its refresh token and cached access token are stored in KV. See `SPOTIFY_SETUP.md` for OAuth setup. The homepage uses Last.fm as a fallback and does not show a timeline without Spotify's real playback progress. Public watchlist and gamelist responses are cached for five minutes and the local Workers Cache entry is cleared after an admin save.

## Data saves

`/api/watchlist`, `/api/gamelist`, `/api/setup` and `/api/wishlist` return:

```json
{
  "items": [],
  "revision": 0
}
```

Admin saves must send the current `revision`. If another tab has already saved a newer version, the Worker returns `409 revision_conflict` with the current server copy. The browser then reloads the current list instead of silently overwriting it.

Existing KV data stored as a plain array is still supported. After the next admin save it is migrated to:

```json
{
  "revision": 1,
  "items": []
}
```

### setup item shape (`setup` key)

```json
{
  "id": "gpu",
  "section": "pc",
  "slot": "Видеокарта",
  "title": "Gigabyte NVIDIA GeForce GTX 650",
  "icon": "gpu",
  "note": "затычка, коплю на новую",
  "link": null,
  "order": 2,
  "updatedAt": 1789936000000
}
```

`section` is `pc` or `gear`; `icon` must be one of `cpu, gpu, ram, board, ssd, hdd, cooler, psu, case,
audio, monitor, keyboard, mouse, mic, other` (anything else is stored as `other`); `link` accepts
`https:` URLs only. `setup.html` ships with `DEFAULT_ITEMS`, so a fresh or unbound KV still renders the
full build instead of an empty page - the first admin save writes those defaults into KV.

### wishlist item shape (`wishlist` key)

```json
{
  "id": "wish_rtx",
  "title": "NVIDIA GeForce RTX 5070 12GB",
  "category": "hardware",
  "status": "saving",
  "priority": 3,
  "price": 68000,
  "currency": "RUB",
  "note": "главная цель",
  "link": "https://example.com/gpu",
  "image": null,
  "addedAt": 1789936000000,
  "updatedAt": 1789936000000
}
```

`category` is `hardware | peripheral | other`, `status` is `want | saving | bought`, `priority` is
`1..3` (out-of-range values fall back to `2`), `currency` is `RUB | USD | EUR` (anything else falls back
to `RUB`). `price` must be positive and is clamped to 1e9 and rounded to cents; otherwise it is stored as
`null` and the UI shows "цена -". `link` and `image` accept `https:` URLs only.

Both collections are write-protected by the same `ADMIN_PASSWORD` session cookie as the other lists, and
both PUTs are throttled by the existing `WRITE_RATE_LIMIT` binding - no new secrets, bindings or
rate limiters are required.

## Security notes

HTML responses get a per-request CSP nonce from `worker.js`, so the site's own inline scripts are still nonce-protected. Tailwind is loaded through `cdn.tailwindcss.com` to preserve the existing design, so the CSP allows Tailwind's runtime requirements. `_headers` keeps a static fallback for non-Worker static serving, but the Worker response headers are the production path.
