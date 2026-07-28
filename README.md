# imdbarr

Serves your IMDb lists as JSON endpoints that Radarr and Sonarr subscribe to as
**Custom Lists**. Python 3 standard library only — no pip, no venv, no npm.

Your four lists are already in `config.json`.

| List | Type | Endpoint |
|---|---|---|
| Movies (`ls4179821413`) | movie | `/list/movies` |
| Anime - Series (`ls4179821814`) | series | `/list/anime-series` |
| TV Shows (`ls4179821216`) | series | `/list/tv-shows` |
| Anime Movies (`ls4179983344`) | movie | `/list/anime-movies` |

---

## How it works

1. Every `refresh_interval_minutes`, it fetches each IMDb list page and pulls out
   the `tt` ids.
2. Each id goes to TMDb to get a `tmdbId` (movies) or `tvdbId` (series), because
   that's what Radarr and Sonarr actually key on. Resolutions are cached in
   `data/idmap.json` forever — an IMDb id never changes what it points to.
3. Results are cached in `data/state.json` and served on the endpoints above.

**If a refresh fails, the last good data keeps being served.** An empty list
handed to Radarr looks like "the user deleted everything", which combined with
Clean Library could remove your films. If a list has never fetched successfully,
the endpoint returns HTTP 503 so Radarr's Test button fails loudly instead.

---

## Install

You need a **TMDb API key** (free): https://www.themoviedb.org/settings/api —
"API Read Access Token" is not what you want; use the shorter **API Key (v3
auth)**. You may already have one from the vault poster art.

```bash
sudo mkdir -p /opt/imdbarr
sudo cp imdbarr.py config.json /opt/imdbarr/
sudo useradd --system --no-create-home --shell /usr/sbin/nologin imdbarr
sudo mkdir -p /opt/imdbarr/data
sudo chown -R imdbarr:imdbarr /opt/imdbarr
sudo nano /opt/imdbarr/config.json     # paste your TMDb key
```

### Check the scraper before installing the service

This is the step that matters. IMDb changes its page structure periodically, so
verify the parser sees your titles:

```bash
cd /opt/imdbarr
sudo -u imdbarr python3 imdbarr.py --debug-list ls4179821413
```

You want output like:

```
__NEXT_DATA__ present: True
next_data     : 42   items (next_data:listItem, hasNextPage=False)
chosen strategy: next_data:listItem
first 15 titles:
  tt0133093    The Matrix
  ...
```

If it finds 0 items, the raw HTML is dumped to `data/ls4179821413.html` so the
page can be inspected directly. `raw_html` as the chosen strategy still works but
is the crude fallback — it may pick up stray links.

Then do a full dry run (no server, just fetch and report):

```bash
sudo -u imdbarr python3 imdbarr.py --once
```

### Run it

```bash
sudo cp imdbarr.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now imdbarr
sudo systemctl status imdbarr
journalctl -u imdbarr -f
```

Status page: `http://<container-ip>:8586/`

---

## Configuration

```jsonc
{
  "port": 8586,
  "refresh_interval_minutes": 360,   // how often to re-scrape IMDb
  "tmdb_api_key": "...",
  "lists": [
    {
      "name": "Movies",              // label on the status page
      "path": "movies",              // becomes /list/movies
      "url": "https://www.imdb.com/list/ls4179821413/",
      "type": "movie",               // "movie" -> Radarr, "series" -> Sonarr
      "enabled": true                // false to stop fetching without deleting
    }
  ]
}
```

Adding a fifth list is four lines and a restart. `url` accepts the full IMDb URL
with or without the `?ref_=` tracking junk, or just the bare `ls...` id.

Don't set `refresh_interval_minutes` below about 60. Scraping is scraping — be
polite to a site that isn't offering you an API. Six hours is plenty for a
watchlist.

### Endpoints

| Path | Purpose |
|---|---|
| `/` | status page: item counts, last refresh, errors |
| `/list/<path>` | the JSON Radarr/Sonarr consumes |
| `/health` | JSON health check, 503 if any list is empty |
| `/refresh` | force an immediate refresh |

---

## Wiring into Radarr and Sonarr

**Radarr** → Settings → Lists → **+** → Advanced List → **Custom Lists**

- List URL: `http://<container-ip>:8586/list/movies`
- Second list for `/list/anime-movies`
- Quality profile, root folder, monitoring, tags: your call

**Sonarr** → Settings → Lists → **+** → Advanced List → **Custom List**

- List URL: `http://<container-ip>:8586/list/tv-shows`
- Second list for `/list/anime-series`

Hit **Test** on each. Use the container IP rather than `localhost` unless the
\*arr runs in the same container.

**Set Clean Library Level to Disabled** in Settings → Lists → Options on both,
unless you specifically want removing a title from IMDb to unmonitor or delete
it locally.

### A note on the JSON shape

Each entry carries both `tmdbId` and `tmdb_id` (and `tvdbId`/`tvdb_id`). That's
deliberate. Radarr and Sonarr's custom list parsers have used both conventions
across versions, and unknown keys are ignored, so emitting both means the list
binds correctly either way rather than silently importing zero items.

---

## Troubleshooting

**Radarr's Test says the list is empty or invalid** — hit `/list/movies` in a
browser. 503 means no successful scrape yet; check `journalctl -u imdbarr`.

**Some titles never appear** — the status page shows an "unmatched" count. Those
are titles TMDb couldn't resolve from the IMDb id, common with obscure anime.
`data/state.json` lists them under `unresolved`. Usually fixable by adding the
IMDb id to the title on TMDb itself.

**IMDb starts returning 403** — they've tightened bot detection. Try a fresher
`user_agent` string in the config first. If that fails, the fallback is exporting
each list to CSV while logged in and pointing the script at those files instead;
that's a small change and worth asking for if it comes to it.

**A list is private** — the scraper is anonymous, so every list must be public.
Open it in a private browser window to check.
