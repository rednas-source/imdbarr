#!/usr/bin/env python3
"""
imdbarr - serve IMDb lists as Radarr/Sonarr Custom Lists.

Scrapes public IMDb lists, resolves each title to TMDb/TVDB ids, and exposes
one JSON endpoint per list that Radarr and Sonarr can subscribe to as a
"Custom List". Refreshes on a configurable interval and always serves the last
known-good data if a refresh fails.

Standard library only. No pip install required.
"""

import argparse
import gzip
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(BASE_DIR, "config.json")

TT_RE = re.compile(r"^tt\d{6,10}$")
TT_IN_URL_RE = re.compile(r"/title/(tt\d{6,10})/")
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)
LD_JSON_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL
)
LIST_ID_RE = re.compile(r"(ls\d{6,12})")

TMDB_BASE = "https://api.themoviedb.org/3"

# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------


def log(msg, *args):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if args:
        msg = msg % args
    print("[%s] %s" % (stamp, msg), flush=True)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    cfg.setdefault("port", 8586)
    cfg.setdefault("bind", "0.0.0.0")
    cfg.setdefault("refresh_interval_minutes", 360)
    cfg.setdefault("data_dir", os.path.join(BASE_DIR, "data"))
    cfg.setdefault("max_pages_per_list", 20)
    cfg.setdefault("page_delay_seconds", 2.0)
    cfg.setdefault("tmdb_delay_seconds", 0.06)
    cfg.setdefault("tmdb_language", "en-US")
    cfg.setdefault(
        "user_agent",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    )

    if not cfg.get("tmdb_api_key"):
        raise SystemExit(
            "config error: tmdb_api_key is required.\n"
            "Get a free key at https://www.themoviedb.org/settings/api"
        )

    lists = cfg.get("lists") or []
    if not lists:
        raise SystemExit("config error: no lists defined.")

    seen_paths = set()
    for entry in lists:
        entry.setdefault("enabled", True)

        raw = entry.get("imdb_list_id") or entry.get("url") or ""
        match = LIST_ID_RE.search(raw)
        if not match:
            raise SystemExit(
                "config error: could not find an IMDb list id (lsXXXXXXXXX) in %r"
                % raw
            )
        entry["imdb_list_id"] = match.group(1)

        kind = (entry.get("type") or "").lower()
        if kind in ("movie", "movies", "film"):
            entry["type"] = "movie"
        elif kind in ("series", "tv", "show", "shows", "tvshow"):
            entry["type"] = "series"
        else:
            raise SystemExit(
                "config error: list %r has type %r; must be 'movie' or 'series'"
                % (entry.get("name"), entry.get("type"))
            )

        if not entry.get("path"):
            entry["path"] = slugify(entry.get("name") or entry["imdb_list_id"])
        if entry["path"] in seen_paths:
            raise SystemExit("config error: duplicate path %r" % entry["path"])
        seen_paths.add(entry["path"])

        if not entry.get("name"):
            entry["name"] = entry["path"]

    return cfg


def slugify(text):
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower())
    return text.strip("-") or "list"


# ---------------------------------------------------------------------------
# http helper
# ---------------------------------------------------------------------------


def http_get(url, user_agent, timeout=30, retries=3, accept="text/html"):
    last_error = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url)
        req.add_header("User-Agent", user_agent)
        req.add_header("Accept", accept)
        req.add_header("Accept-Language", "en-US,en;q=0.9")
        req.add_header("Accept-Encoding", "gzip, identity")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            last_error = "HTTP %s" % exc.code
            if exc.code in (403, 404, 401):
                break
        except Exception as exc:  # noqa: BLE001 - surface anything to the caller
            last_error = str(exc)
        if attempt < retries:
            time.sleep(2 * attempt)
    raise RuntimeError("GET %s failed: %s" % (url, last_error))


# ---------------------------------------------------------------------------
# IMDb parsing
# ---------------------------------------------------------------------------


def walk(obj):
    """Yield every dict nested anywhere inside obj, in document order.

    Breadth-first via a queue - a LIFO stack would reverse sibling order and
    scramble the list ordering IMDb gives us.
    """
    queue = deque([obj])
    while queue:
        node = queue.popleft()
        if isinstance(node, dict):
            yield node
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)


def _title_from_node(node):
    text = node.get("titleText")
    if isinstance(text, dict):
        return text.get("text")
    if isinstance(text, str):
        return text
    original = node.get("originalTitleText")
    if isinstance(original, dict):
        return original.get("text")
    return None


def _add(found, order, imdb_id, title=None):
    if not imdb_id or not TT_RE.match(imdb_id):
        return
    if imdb_id not in found:
        found[imdb_id] = title
        order.append(imdb_id)
    elif title and not found[imdb_id]:
        found[imdb_id] = title


def extract_from_next_data(page_html):
    """Strategies 1-3, all working off the embedded __NEXT_DATA__ blob."""
    match = NEXT_DATA_RE.search(page_html)
    if not match:
        return [], None, None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return [], None, None

    found, order = {}, []

    # Strategy 1: the documented shape - list items carry a `listItem` object.
    for node in walk(data):
        item = node.get("listItem")
        if isinstance(item, dict):
            _add(found, order, item.get("id"), _title_from_node(item))
    if order:
        return _pack(found, order), "next_data:listItem", _has_next_page(data)

    # Strategy 2: generic GraphQL edges -> node.
    for node in walk(data):
        inner = node.get("node")
        if isinstance(inner, dict) and isinstance(inner.get("id"), str):
            _add(found, order, inner.get("id"), _title_from_node(inner))
    if order:
        return _pack(found, order), "next_data:edges", _has_next_page(data)

    # Strategy 3: anything that looks like a title object.
    for node in walk(data):
        node_id = node.get("id")
        if isinstance(node_id, str) and TT_RE.match(node_id) and "titleText" in node:
            _add(found, order, node_id, _title_from_node(node))
    if order:
        return _pack(found, order), "next_data:titleText", _has_next_page(data)

    return [], None, None


def _has_next_page(data):
    for node in walk(data):
        info = node.get("pageInfo")
        if isinstance(info, dict) and "hasNextPage" in info:
            return bool(info.get("hasNextPage"))
    return None


def _pack(found, order):
    return [{"imdb_id": tt, "title": found.get(tt)} for tt in order]


def extract_from_ld_json(page_html):
    """Strategy 4: the JSON-LD ItemList block IMDb ships for SEO."""
    found, order = {}, []
    for blob in LD_JSON_RE.findall(page_html):
        try:
            data = json.loads(html.unescape(blob))
        except json.JSONDecodeError:
            continue
        for node in walk(data):
            elements = node.get("itemListElement")
            if not isinstance(elements, list):
                continue
            for element in elements:
                if not isinstance(element, dict):
                    continue
                item = element.get("item") if isinstance(element.get("item"), dict) else element
                url = item.get("url") or ""
                title = item.get("name")
                match = TT_IN_URL_RE.search(url)
                if match:
                    _add(found, order, match.group(1), title)
    return _pack(found, order)


def extract_from_raw_html(page_html):
    """Strategy 5: last resort - every /title/ttXXXXXXX/ link on the page."""
    found, order = {}, []
    for tt in TT_IN_URL_RE.findall(page_html):
        _add(found, order, tt, None)
    return _pack(found, order)


def parse_list_page(page_html):
    items, strategy, has_next = extract_from_next_data(page_html)
    if items:
        return items, strategy, has_next

    items = extract_from_ld_json(page_html)
    if items:
        return items, "ld_json", None

    items = extract_from_raw_html(page_html)
    if items:
        return items, "raw_html", None

    return [], "none", None


def fetch_imdb_list(list_id, cfg, verbose=False):
    """Walk every page of an IMDb list and return ordered {imdb_id, title} dicts."""
    collected, seen = [], set()
    strategies = []
    page = 1
    max_pages = cfg["max_pages_per_list"]

    while page <= max_pages:
        url = "https://www.imdb.com/list/%s/" % list_id
        if page > 1:
            url += "?page=%d" % page

        page_html = http_get(url, cfg["user_agent"])
        items, strategy, has_next = parse_list_page(page_html)
        strategies.append(strategy)

        if verbose:
            log("  page %d: %d items via %s (hasNextPage=%s)",
                page, len(items), strategy, has_next)

        fresh = [item for item in items if item["imdb_id"] not in seen]
        for item in fresh:
            seen.add(item["imdb_id"])
            collected.append(item)

        # Stop when the page gave us nothing new - either the list ended or
        # IMDb ignored the page parameter and handed back page 1 again.
        if not fresh:
            break
        if has_next is False:
            break

        page += 1
        time.sleep(cfg["page_delay_seconds"])

    if not collected:
        raise RuntimeError(
            "no titles found on list %s - the list may be private, empty, or "
            "IMDb changed its page structure (run with --debug-list to inspect)"
            % list_id
        )

    return collected, strategies


# ---------------------------------------------------------------------------
# TMDb resolution
# ---------------------------------------------------------------------------


class Resolver:
    """imdb_id -> {tmdb_id, tvdb_id, title, year}, cached to disk forever."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.path = os.path.join(cfg["data_dir"], "idmap.json")
        self.lock = threading.Lock()
        self.cache = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    self.cache = json.load(fh)
            except Exception as exc:  # noqa: BLE001
                log("warning: could not read id cache (%s), starting fresh", exc)

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.cache, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def _api(self, path, params=None):
        params = dict(params or {})
        params["api_key"] = self.cfg["tmdb_api_key"]
        url = "%s%s?%s" % (TMDB_BASE, path, urllib.parse.urlencode(params))
        body = http_get(url, self.cfg["user_agent"], accept="application/json")
        return json.loads(body)

    def resolve(self, imdb_id, kind):
        key = "%s:%s" % (imdb_id, kind)
        with self.lock:
            if key in self.cache:
                return self.cache[key]

        result = self._resolve_uncached(imdb_id, kind)
        with self.lock:
            self.cache[key] = result
        time.sleep(self.cfg["tmdb_delay_seconds"])
        return result

    def _resolve_uncached(self, imdb_id, kind):
        try:
            found = self._api(
                "/find/%s" % imdb_id,
                {"external_source": "imdb_id", "language": self.cfg["tmdb_language"]},
            )
        except Exception as exc:  # noqa: BLE001
            log("  tmdb lookup failed for %s: %s", imdb_id, exc)
            return None

        if kind == "movie":
            results = found.get("movie_results") or []
            if not results:
                return None
            movie = results[0]
            date = movie.get("release_date") or ""
            return {
                "imdb_id": imdb_id,
                "tmdb_id": movie.get("id"),
                "title": movie.get("title") or movie.get("original_title"),
                "year": int(date[:4]) if date[:4].isdigit() else None,
            }

        results = found.get("tv_results") or []
        if not results:
            return None
        show = results[0]
        tmdb_id = show.get("id")
        tvdb_id = None
        try:
            external = self._api("/tv/%s/external_ids" % tmdb_id)
            tvdb_id = external.get("tvdb_id")
        except Exception as exc:  # noqa: BLE001
            log("  tvdb lookup failed for %s: %s", imdb_id, exc)

        date = show.get("first_air_date") or ""
        return {
            "imdb_id": imdb_id,
            "tmdb_id": tmdb_id,
            "tvdb_id": tvdb_id,
            "title": show.get("name") or show.get("original_name"),
            "year": int(date[:4]) if date[:4].isdigit() else None,
        }


# ---------------------------------------------------------------------------
# output shaping
# ---------------------------------------------------------------------------


def to_radarr(record):
    """Radarr Custom List entry. Both key styles on purpose - see README."""
    if not record or not record.get("tmdb_id"):
        return None
    entry = {
        "title": record.get("title"),
        "tmdbId": record["tmdb_id"],
        "tmdb_id": record["tmdb_id"],
        "imdbId": record["imdb_id"],
        "imdb_id": record["imdb_id"],
    }
    if record.get("year"):
        entry["year"] = record["year"]
    return entry


def to_sonarr(record):
    if not record or not record.get("tvdb_id"):
        return None
    entry = {
        "title": record.get("title"),
        "tvdbId": record["tvdb_id"],
        "tvdb_id": record["tvdb_id"],
        "imdbId": record["imdb_id"],
        "imdb_id": record["imdb_id"],
    }
    if record.get("tmdb_id"):
        entry["tmdbId"] = record["tmdb_id"]
    if record.get("year"):
        entry["year"] = record["year"]
    return entry


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        self.path = os.path.join(cfg["data_dir"], "state.json")
        self.lock = threading.Lock()
        self.state = {}
        self.refreshing = False
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    self.state = json.load(fh)
                log("loaded cached state for %d list(s)", len(self.state))
            except Exception as exc:  # noqa: BLE001
                log("warning: could not read state (%s), starting fresh", exc)

    def save(self):
        tmp = self.path + ".tmp"
        with self.lock:
            snapshot = json.dumps(self.state, indent=2)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(snapshot)
        os.replace(tmp, self.path)

    def get(self, path):
        with self.lock:
            return self.state.get(path)

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def record_success(self, path, entry, items, unresolved, strategies):
        with self.lock:
            self.state[path] = {
                "name": entry["name"],
                "type": entry["type"],
                "imdb_list_id": entry["imdb_list_id"],
                "items": items,
                "count": len(items),
                "unresolved": unresolved,
                "strategies": strategies,
                "last_success": datetime.now(timezone.utc).isoformat(),
                "last_attempt": datetime.now(timezone.utc).isoformat(),
                "error": None,
            }

    def record_failure(self, path, entry, error):
        with self.lock:
            previous = self.state.get(path)
            if previous:
                previous["last_attempt"] = datetime.now(timezone.utc).isoformat()
                previous["error"] = error
            else:
                self.state[path] = {
                    "name": entry["name"],
                    "type": entry["type"],
                    "imdb_list_id": entry["imdb_list_id"],
                    "items": [],
                    "count": 0,
                    "unresolved": [],
                    "strategies": [],
                    "last_success": None,
                    "last_attempt": datetime.now(timezone.utc).isoformat(),
                    "error": error,
                }


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


def refresh_one(entry, cfg, resolver, store, verbose=False):
    log("refreshing %r (%s, %s)", entry["name"], entry["type"], entry["imdb_list_id"])
    try:
        scraped, strategies = fetch_imdb_list(entry["imdb_list_id"], cfg, verbose)
    except Exception as exc:  # noqa: BLE001
        log("  FAILED: %s", exc)
        store.record_failure(entry["path"], entry, str(exc))
        return False

    log("  scraped %d title(s) from IMDb", len(scraped))

    shape = to_radarr if entry["type"] == "movie" else to_sonarr
    items, unresolved = [], []
    for item in scraped:
        record = resolver.resolve(item["imdb_id"], entry["type"])
        shaped = shape(record)
        if shaped:
            items.append(shaped)
        else:
            unresolved.append(
                {"imdb_id": item["imdb_id"], "title": item.get("title")}
            )

    if not items:
        message = "scraped %d titles but resolved none" % len(scraped)
        log("  FAILED: %s", message)
        store.record_failure(entry["path"], entry, message)
        return False

    store.record_success(entry["path"], entry, items, unresolved, strategies)
    log("  ok: %d resolved, %d unresolved", len(items), len(unresolved))
    if unresolved:
        for miss in unresolved[:10]:
            log("    unresolved: %s (%s)", miss["imdb_id"], miss.get("title") or "?")
    return True


def refresh_all(cfg, resolver, store, verbose=False):
    if store.refreshing:
        log("refresh already in progress, skipping")
        return
    store.refreshing = True
    try:
        active = [entry for entry in cfg["lists"] if entry.get("enabled", True)]
        log("=== refresh started (%d list(s)) ===", len(active))
        for entry in active:
            refresh_one(entry, cfg, resolver, store, verbose)
        resolver.save()
        store.save()
        log("=== refresh finished ===")
    finally:
        store.refreshing = False


def refresh_loop(cfg, resolver, store):
    interval = max(5, int(cfg["refresh_interval_minutes"])) * 60
    while True:
        time.sleep(interval)
        try:
            refresh_all(cfg, resolver, store)
        except Exception as exc:  # noqa: BLE001
            log("refresh loop error: %s", exc)


# ---------------------------------------------------------------------------
# http server
# ---------------------------------------------------------------------------

STATUS_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>imdbarr</title>
<style>
body{background:#0d0f12;color:#e6e6e6;font:14px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:40px}
h1{font-size:20px;font-weight:600;margin:0 0 4px}
.sub{color:#8a8f98;margin-bottom:28px}
table{border-collapse:collapse;width:100%%;max-width:1000px}
th,td{text-align:left;padding:10px 14px;border-bottom:1px solid #1e2228}
th{color:#8a8f98;font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.05em}
code{background:#171a1f;padding:2px 6px;border-radius:4px;color:#9ecbff}
.ok{color:#4ade80}.bad{color:#f87171}.warn{color:#fbbf24}
a{color:#9ecbff}
</style></head><body>
<h1>imdbarr</h1>
<div class="sub">IMDb lists served as Radarr / Sonarr custom lists &middot; refresh every %(interval)s min</div>
<table><tr><th>List</th><th>Type</th><th>Items</th><th>Endpoint</th><th>Last success</th><th>Status</th></tr>
%(rows)s
</table>
<p class="sub" style="margin-top:28px"><a href="/refresh">Force refresh now</a></p>
</body></html>"""


def make_handler(cfg, resolver, store):
    endpoints = {entry["path"]: entry for entry in cfg["lists"]}

    class Handler(BaseHTTPRequestHandler):
        server_version = "imdbarr/1.0"

        def log_message(self, fmt, *args):  # quieter access log
            log("http %s - %s", self.address_string(), fmt % args)

        def _send(self, code, body, content_type="application/json"):
            payload = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):  # noqa: N802 - required name
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"

            if path == "/":
                return self._status()
            if path == "/health":
                return self._health()
            if path == "/refresh":
                threading.Thread(
                    target=refresh_all, args=(cfg, resolver, store), daemon=True
                ).start()
                return self._send(202, json.dumps({"status": "refresh started"}))
            if path.startswith("/list/"):
                return self._list(path[len("/list/"):])

            self._send(404, json.dumps({"error": "not found"}))

        def _list(self, name):
            if name not in endpoints:
                return self._send(
                    404,
                    json.dumps(
                        {"error": "unknown list", "available": sorted(endpoints)}
                    ),
                )
            state = store.get(name)
            if not state or not state.get("items"):
                # Never hand Radarr an empty array - it looks like a valid but
                # emptied list. Fail loudly instead.
                return self._send(
                    503,
                    json.dumps(
                        {
                            "error": "no data yet",
                            "detail": (state or {}).get("error"),
                        }
                    ),
                )
            return self._send(200, json.dumps(state["items"], indent=2))

        def _health(self):
            snap = store.snapshot()
            healthy = all(item.get("count") for item in snap.values()) and snap
            body = {
                "status": "ok" if healthy else "degraded",
                "lists": {
                    key: {
                        "count": val.get("count", 0),
                        "last_success": val.get("last_success"),
                        "error": val.get("error"),
                    }
                    for key, val in snap.items()
                },
            }
            return self._send(200 if healthy else 503, json.dumps(body, indent=2))

        def _status(self):
            snap = store.snapshot()
            rows = []
            host = self.headers.get("Host", "localhost:%s" % cfg["port"])
            for key, entry in endpoints.items():
                state = snap.get(key) or {}
                count = state.get("count", 0)
                error = state.get("error")
                if error:
                    status = '<span class="bad">%s</span>' % html.escape(error[:80])
                elif count:
                    status = '<span class="ok">ok</span>'
                else:
                    status = '<span class="warn">not fetched yet</span>'
                unresolved = len(state.get("unresolved") or [])
                count_cell = str(count)
                if unresolved:
                    count_cell += ' <span class="warn">(+%d unmatched)</span>' % unresolved
                rows.append(
                    "<tr><td>%s</td><td>%s</td><td>%s</td>"
                    "<td><code>http://%s/list/%s</code></td><td>%s</td><td>%s</td></tr>"
                    % (
                        html.escape(entry["name"]),
                        entry["type"],
                        count_cell,
                        html.escape(host),
                        key,
                        (state.get("last_success") or "never")[:19].replace("T", " "),
                        status,
                    )
                )
            page = STATUS_PAGE % {
                "interval": cfg["refresh_interval_minutes"],
                "rows": "\n".join(rows),
            }
            self._send(200, page, "text/html")

    return Handler


# ---------------------------------------------------------------------------
# debug mode
# ---------------------------------------------------------------------------


def debug_list(list_id, cfg):
    """Fetch page 1 of a list and report what each strategy found."""
    match = LIST_ID_RE.search(list_id)
    list_id = match.group(1) if match else list_id
    url = "https://www.imdb.com/list/%s/" % list_id
    print("fetching %s\n" % url)

    page_html = http_get(url, cfg["user_agent"])
    dump = os.path.join(cfg["data_dir"], "%s.html" % list_id)
    with open(dump, "w", encoding="utf-8") as fh:
        fh.write(page_html)
    print("raw html: %d bytes -> %s\n" % (len(page_html), dump))

    print("__NEXT_DATA__ present: %s" % bool(NEXT_DATA_RE.search(page_html)))
    print("ld+json blocks: %d" % len(LD_JSON_RE.findall(page_html)))
    print("/title/tt links: %d\n" % len(set(TT_IN_URL_RE.findall(page_html))))

    next_items, strategy, has_next = extract_from_next_data(page_html)
    print("next_data     : %-4d items (%s, hasNextPage=%s)"
          % (len(next_items), strategy, has_next))
    print("ld_json       : %-4d items" % len(extract_from_ld_json(page_html)))
    print("raw_html      : %-4d items" % len(extract_from_raw_html(page_html)))

    items, chosen, _ = parse_list_page(page_html)
    print("\nchosen strategy: %s" % chosen)
    print("first 15 titles:")
    for item in items[:15]:
        print("  %-12s %s" % (item["imdb_id"], item.get("title") or "-"))
    if len(items) > 15:
        print("  ... and %d more" % (len(items) - 15))


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Serve IMDb lists to Radarr/Sonarr")
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true",
                        help="refresh once, print results, exit (no server)")
    parser.add_argument("--debug-list", metavar="LIST_ID",
                        help="inspect one IMDb list and show parser output")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    os.makedirs(cfg["data_dir"], exist_ok=True)

    if args.debug_list:
        debug_list(args.debug_list, cfg)
        return

    resolver = Resolver(cfg)
    store = Store(cfg)

    if args.once:
        refresh_all(cfg, resolver, store, verbose=True)
        snap = store.snapshot()
        print()
        for key, val in snap.items():
            print("%-16s %-7s %4d items  %s"
                  % (key, val["type"], val["count"], val.get("error") or "ok"))
        return

    threading.Thread(
        target=refresh_all, args=(cfg, resolver, store, args.verbose), daemon=True
    ).start()
    threading.Thread(
        target=refresh_loop, args=(cfg, resolver, store), daemon=True
    ).start()

    handler = make_handler(cfg, resolver, store)
    server = ThreadingHTTPServer((cfg["bind"], int(cfg["port"])), handler)
    log("listening on http://%s:%s", cfg["bind"], cfg["port"])
    for entry in cfg["lists"]:
        if entry.get("enabled", True):
            log("  %-16s -> /list/%s", entry["name"], entry["path"])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
