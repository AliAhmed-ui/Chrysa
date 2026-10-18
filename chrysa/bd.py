#!/usr/bin/env python3
"""bd: search torlink's sources, pick a result, hand its magnet to a downloader.

Chrysa imports :func:`search_all` for its Browse page; the command line below still works
(``python3 -m chrysa.bd "query"`` or the ``bd.py`` shim in the project root).

Sources (ported from src/sources/, FitGirl left out): YTS, The Pirate Bay,
1337x, EZTV, Nyaa, SubsPlease, BitTorrented. Standard library only.

Run it with:  python3 bd.py "query"

Downloader hand-off, first match wins:
  --cmd "aria2c {magnet}"   any command; {magnet} is replaced (quoted) by the link
  torlink's `serve` API     if `torlnk serve` is running (POST /add on :9161)
  npx torlnk <magnet>       torlink's own TUI, which starts the download
"""
import argparse
import concurrent.futures as cf
import gzip
import html
import json
import re
import shlex
import subprocess
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = "torlink (+https://www.npmjs.com/package/torlnk)"  # same UA the repo sends
TIMEOUT = 15

TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.dler.org:6969/announce",
]


def magnet(info_hash, name):
    tr = "".join("&tr=" + urllib.parse.quote(t, safe="") for t in TRACKERS)
    return f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(name, safe='')}{tr}"


RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


def get(url, accept=None, retries=1):
    """GET with the repo's retry rule: back off and retry transient statuses and network errors."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, **({"Accept": accept} if accept else {})})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_STATUS or attempt == retries:
                raise
        except OSError:
            if attempt == retries:
                raise
        time.sleep(random.uniform(0, 0.5 * 2 ** attempt))


def get_json(url):
    return json.loads(get(url, "application/json"))


def parse_size(text):
    m = re.match(r"\s*([\d.,]+)\s*([KMGT]?)i?B", text or "", re.I)
    if not m:
        return 0
    mult = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}[m.group(2).upper()]
    return int(float(m.group(1).replace(",", "")) * mult)


def result(source, h, name, size, seeds, leech, mag=None):
    return {"source": source, "name": name, "size": size, "seeders": seeds,
            "leechers": leech, "magnet": mag or magnet(h, name)}


# --- sources ---------------------------------------------------------------

def yts(q):
    out = []
    for host in ("yts.mx", "yts.am", "yts.rs"):
        try:
            data = get_json(f"https://{host}/api/v2/list_movies.json?" +
                            urllib.parse.urlencode({"limit": 50, "query_term": q}))
            break
        except Exception:
            data = None
    if data is None:
        raise RuntimeError("YTS unreachable")
    for mv in (data.get("data") or {}).get("movies") or []:
        base = mv.get("title_long") or mv.get("title") or "Unknown"
        for t in mv.get("torrents") or []:
            if not t.get("hash"):
                continue
            tag = " ".join(x for x in (t.get("quality"), t.get("type")) if x)
            name = f"{base} [{tag}]" if tag else base
            out.append(result("YTS", t["hash"].lower(), name, t.get("size_bytes") or 0,
                              t.get("seeds") or 0, t.get("peers") or 0))
    return out


def tpb(q):
    wanted = {201, 202, 205, 207, 208, 209}  # movies + TV categories
    url = "https://apibay.org/q.php?q=" + urllib.parse.quote(q)
    items = get_json(url)
    if len(items) == 1 and items[0].get("id") == "0":  # apibay's no-results sentinel
        items = get_json(url + "&cat=0")
    out = []
    for it in items:
        h = (it.get("info_hash") or "").lower()
        if not h or set(h) == {"0"} or it.get("id") == "0":
            continue
        if int(it.get("category") or 0) not in wanted:
            continue
        out.append(result("TPB", h, it.get("name") or "Unknown", int(it.get("size") or 0),
                          int(it.get("seeders") or 0), int(it.get("leechers") or 0)))
    return out


def x1337_cat(q, cat):
    html_text = base = None
    for i, host in enumerate(("1337x.to", "1337x.st", "x1337x.ws", "1337xx.to")):
        try:
            path = f"/category-search/{urllib.parse.quote(q).replace('%20', '+')}/{cat}/1/"
            html_text = get(f"https://{host}{path}", retries=2 if i == 0 else 0)
            base = f"https://{host}"
            break
        except Exception:
            continue
    if html_text is None:
        raise RuntimeError("1337x unreachable")
    start = html_text.find("table-list")
    if start < 0:
        return []
    rows = []
    for tr in re.split(r"<tr[\s>]", html_text[start:], flags=re.I)[1:]:
        link = re.search(r'href="(/torrent/[^"]+)"[^>]*>([^<]+)</a>', tr, re.I)
        if not link:
            continue
        num = lambda cls: int((re.search(rf'class="{cls}[^"]*">\s*(\d+)', tr, re.I) or [0, 0])[1])
        size = re.search(r'class="coll-4 size[^"]*">\s*([\d.]+\s*[KMGT]i?B)', tr, re.I)
        rows.append((html.unescape(link.group(2).strip()), link.group(1),
                     num("coll-2 seeds"), num("coll-3 leeches"), parse_size(size.group(1) if size else "")))
    # Same relevance filter as the repo: every non-stopword query token must be in the title.
    stop = {"the", "a", "an", "of", "and", "or", "to"}
    tokens = q.lower().split()
    need = [t for t in tokens if t not in stop] or tokens
    rows = [r for r in rows if all(t in r[0].lower() for t in need)]
    rows.sort(key=lambda r: -r[2])

    def detail(row):
        name, path, s, l, size = row
        try:
            m = re.search(r"magnet:\?xt=urn:btih:[^\"'<>\s]+", get(base + path), re.I)
        except Exception:
            return None
        return result("1337x", "", name, size, s, l, html.unescape(m.group(0))) if m else None

    with cf.ThreadPoolExecutor(4) as ex:  # the repo also caps detail-page fetches at 4
        return [r for r in ex.map(detail, rows[:4]) if r]


def x1337(q):
    out, err = [], None
    for cat in ("Movies", "TV"):
        try:
            out += x1337_cat(q, cat)
        except Exception as e:
            err = e
    if not out and err:
        raise err
    return out


def eztv(q):
    # EZTV's API has no search, so fetch the latest feed and filter locally.
    data = get_json("https://eztvx.to/api/get-torrents?limit=100&page=1")
    words = q.lower().split()
    out = []
    for t in data.get("torrents") or []:
        name = t.get("title") or t.get("filename") or ""
        h = (t.get("hash") or "").lower()
        if not h or not all(w in name.lower() for w in words):
            continue
        out.append(result("EZTV", h, name, int(t.get("size_bytes") or 0),
                          t.get("seeds") or 0, t.get("peers") or 0, t.get("magnet_url") or None))
    return out


def nyaa(q):
    xml = get("https://nyaa.si/?" + urllib.parse.urlencode({"page": "rss", "q": q, "c": "0_0", "f": "0"}))
    ns = {"n": "https://nyaa.si/xmlns/nyaa"}
    out = []
    for item in ET.fromstring(xml).iter("item"):
        h = (item.findtext("n:infoHash", "", ns) or "").lower()
        name = item.findtext("title", "") or ""
        if not h or not name:
            continue
        out.append(result("Nyaa", h, name, parse_size(item.findtext("n:size", "", ns)),
                          int(item.findtext("n:seeders", "0", ns) or 0),
                          int(item.findtext("n:leechers", "0", ns) or 0)))
    return out


def subsplease(q):
    data = get_json("https://subsplease.org/api/?" + urllib.parse.urlencode({"f": "search", "s": q, "tz": "UTC"}))
    if not isinstance(data, dict):
        return []
    out = []
    for e in data.values():
        dls = [d for d in e.get("downloads") or [] if d.get("magnet")]
        best = next((d for r in ("1080", "720", "480") for d in dls if d.get("res") == r), dls[0] if dls else None)
        if not best:
            continue
        ep = f" - {e['episode']}" if e.get("episode") else ""
        xl = re.search(r"[?&]xl=(\d+)", best["magnet"])
        out.append(result("SubsPlease", "", f"{e.get('show', 'Unknown')}{ep} [{best.get('res', '?')}p]",
                          int(xl.group(1)) if xl else 0, 0, 0, best["magnet"]))
    return out


def bittorrented(q):
    if len(q) < 3:
        return []
    data = get_json("https://bittorrented.com/api/search/torrents?" + urllib.parse.urlencode(
        {"q": q, "type": "video", "limit": 50, "sortBy": "seeders", "sortOrder": "desc"}))
    out = []
    for r in data.get("results") or []:
        h = (r.get("torrent_infohash") or "").lower()
        if not re.fullmatch(r"[a-f0-9]{40}", h):
            continue
        out.append(result("BitTorrented", h, r.get("torrent_name") or h, r.get("torrent_total_size") or 0,
                          r.get("torrent_seeders") or 0, r.get("torrent_leechers") or 0))
    return out


SOURCES = [("YTS", yts), ("TPB", tpb), ("1337x", x1337), ("EZTV", eztv),
           ("Nyaa", nyaa), ("SubsPlease", subsplease), ("BitTorrented", bittorrented)]


# --- search + select -------------------------------------------------------

def search_all(q, log=None):
    """Query every source in parallel; return de-duplicated results, most seeded first.

    ``log(source, message)`` is told how each source went (default: nothing).
    """
    log = log or (lambda source, message: None)
    results = []
    with cf.ThreadPoolExecutor(len(SOURCES)) as ex:
        futs = {ex.submit(fn, q): name for name, fn in SOURCES}
        for f in cf.as_completed(futs):
            try:
                found = f.result()
                log(futs[f], f"{len(found)} results")
                results += found
            except Exception as e:
                log(futs[f], f"offline ({e})")
    seen, unique = set(), []
    for r in sorted(results, key=lambda r: -r["seeders"]):
        h = re.search(r"btih:([a-z0-9]+)", r["magnet"], re.I)
        key = h.group(1).lower() if h else r["magnet"]
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def choose(results, limit):
    shown = results[:limit]
    for i, r in enumerate(shown, 1):
        print(f"{i:>3}. [{r['source']}] {r['name'][:70]}  {human(r['size'])}  S:{r['seeders']} L:{r['leechers']}")
    while True:
        raw = input("\nNumber to download (blank to quit): ").strip()
        if not raw:
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(shown):
            return shown[int(raw) - 1]
        print("Not a valid number.")


# --- downloader hand-off ---------------------------------------------------

def download(mag, cmd, api):
    if cmd:
        return subprocess.call(cmd.replace("{magnet}", shlex.quote(mag)), shell=True)
    try:
        req = urllib.request.Request(api, data=json.dumps({"magnet": mag}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            print(f"Queued via torlink serve API: {r.read().decode()}")
            return 0
    except urllib.error.HTTPError as e:
        print(f"torlink serve API rejected the magnet: {e.code} {e.read().decode()}")
        return 1
    except OSError:
        pass  # nothing listening; fall back to launching torlink itself
    print("No `torlnk serve` running; launching torlink with the magnet...")
    return subprocess.call(["npx", "torlnk", mag])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", nargs="*", help="what to search for (prompted if omitted)")
    ap.add_argument("--cmd", help='downloader command with {magnet}, e.g. "aria2c {magnet}"')
    ap.add_argument("--api", default="http://127.0.0.1:9161/add", help="torlink serve add endpoint")
    ap.add_argument("--limit", type=int, default=25, help="max results to list")
    args = ap.parse_args()

    q = " ".join(args.query).strip() or input("Search: ").strip()
    if not q:
        return 1
    print(f"Searching for {q!r}...", file=sys.stderr)
    results = search_all(q, lambda source, message: print(f"  {source}: {message}", file=sys.stderr))
    if not results:
        print("No results.")
        return 1
    pick = choose(results, args.limit)
    if not pick:
        return 0
    print(f"\nDownloading: {pick['name']}")
    return download(pick["magnet"], args.cmd, args.api)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        sys.exit(130)
