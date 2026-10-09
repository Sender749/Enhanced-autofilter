# Web app (Telegram Mini App)

Netflix-style catalog of the bot's files: posters, latest on top, search, series with seasons/episodes,
stream / download in the app, or send to the bot PM.

## Turn it on
Nothing extra is required if Fast Download already works: the app is served by the same service at
`<STREAM_BASE_URL>/app`. Optional env vars:

| Var | Default | Meaning |
|---|---|---|
| `WEBAPP_ENABLED` | `true` | master switch |
| `WEBAPP_URL` | `STREAM_BASE_URL` | public https URL of this deployment |
| `WEBAPP_SYNC_SECONDS` | `600` | how often new files are added to the catalog |

On boot the bot sets a "Movies" menu button; `/app` also sends an Open button.

## How it works
- `webapp_items` / `webapp_titles` collections are built from your files collection (it is only read).
  Captions are parsed by `webapp/parser.py` (season, episode ranges, packs, quality, languages).
- New files are picked up every `WEBAPP_SYNC_SECONDS`. Posters/ratings come from TMDB in the background,
  newest titles first. No confident match -> styled fallback card with the file name.
- Home rows: Recently added (upload time) and New releases (TMDB release date) both newest first.
- Stream/Download reuse Fast Download (BIN copy, signed link, daily quota). "Send to bot PM" reuses
  `deliver_file`, so premium / force-sub / limit / verify rules still apply.

## Admin commands
- `/webapp_status` - parsed files, titles, posters pending / not found
- `/webapp_sync` - add new files now; `/webapp_sync full` - re-parse everything (after parser changes)

## Notes
- Opening `/app` outside Telegram shows "Open this app from the bot" (all API calls need signed initData).
- Browsers can't play every MKV audio codec; the player offers VLC / MX Player / player-page fallbacks.
