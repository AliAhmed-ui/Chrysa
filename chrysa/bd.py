#!/usr/bin/env python3
"""bd: search torlink's sources, pick a result, hand its magnet to a downloader.

Chrysa imports :data:`SOURCES` (through :mod:`chrysa.sources`) for its Browse page; the command line below still
works (``python3 -m chrysa.bd "query"`` or the ``bd.py`` shim in the project root).

Sources: YTS, The Pirate Bay, 1337x, LimeTorrents, EZTV, Nyaa, SubsPlease. Standard library only.

Every site's hosts, page markers and row patterns live in :data:`SCHEMA`, so a site that changes its HTML or domain
is fixed in one place. Fetching and parsing are separate (``parse_*`` take the raw page), which is what the
contract tests in ``tests/test_sources.py`` exercise. A source that fails raises :class:`SourceError` carrying one
of the statuses below instead of quietly returning nothing.

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
import socket
import subprocess
import random
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = "torlink (+https://www.npmjs.com/package/torlnk)"  # same UA the repo sends
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
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

# --- what each site looks like ---------------------------------------------
# ``version`` is bumped whenever a parser is changed to follow a site, so diagnostics show which parser saw what.
# ``hosts`` are tried in order (the last one that worked first); ``markers`` must appear on a genuine page.

SCHEMA = {
    "YTS": {"version": 1, "hosts": ["yts.mx", "yts.am", "yts.rs"], "timeout": 12},
    "TPB": {"version": 1, "hosts": ["apibay.org"], "timeout": 12},
    "1337x": {
        "version": 2, "hosts": ["1337x.la", "1337x.to", "1337x.st"], "timeout": 12,
        "markers": ["search-page"],  # the search results column; present even when nothing matched
        "row_link": r'href="(/torrent/[^"]+)"[^>]*>([^<]+)</a>',
        "seeds": r'class="coll-2 seeds[^"]*">\s*(\d+)',
        "leeches": r'class="coll-3 leeches[^"]*">\s*(\d+)',
        "size": r'class="coll-4 size[^"]*">\s*([\d.,]+\s*[KMGT]i?B)',
        "magnet": r"magnet:\?xt=urn:btih:[^\"'<>\s]+",
    },
    "LimeTorrents": {
        "version": 2, "hosts": ["www.limetorrents.fun", "www.limetorrents.lol", "www.limetorrents.info"], "timeout": 12,
        "markers": ["tt-name"], "empty": ["No results found"],
        "row": r'-torrent-\d+\.html',  # a genuine result links its detail page; injected ad rows do not
        "hash": r"torrent/([A-Fa-f0-9]{40})\.torrent",
        "name": r'class="tt-name">.*?<a href="/[^"]*-torrent-\d+\.html">([^<]+)</a>',
        "size": r'class="tdnormal">([\d.,]+\s*[KMGT]?B)<',
        "seeds": r'class="tdseed">([\d,]+)',
        "leech": r'class="tdleech">([\d,]+)',
    },
    "EZTV": {"version": 1, "hosts": ["eztvx.to"], "timeout": 12},
    "Nyaa": {"version": 1, "hosts": ["nyaa.si"], "timeout": 10},
    "SubsPlease": {"version": 1, "hosts": ["subsplease.org"], "timeout": 10},
}

# --- failure kinds ---------------------------------------------------------

OFFLINE = "offline"  # DNS failure, refused/reset connection, 5xx
CHANGED = "changed"  # the page no longer has the structure the parser expects
PARSE_FAILURE = "parse failure"  # the parser itself crashed on the page
TIMEOUT = "timeout"
BLOCKED = "blocked"  # anti-bot challenge, 403, rate limiting
INVALID_RESPONSE = "invalid response"  # not JSON / not XML / HTML where data was expected
NO_RESULTS = "no results"


class SourceError(RuntimeError):
    def __init__(self, status, detail=""):
        super().__init__(f"{status}: {detail}" if detail else status)
        self.status, self.detail = status, detail


# Only what interstitials carry: Cloudflare injects "challenge-platform" scripts into ordinary pages too.
_CHALLENGE = re.compile(
    r"<title>\s*(?:Just a moment\.\.\.|Attention Required! \| Cloudflare|DDoS-Guard|Checking your browser)|"
    r"cf-browser-verification|window\._cf_chl_opt|id=\"challenge-form\"", re.I)


def is_challenge(text):
    return bool(_CHALLENGE.search(text[:30000]))


def classify_error(error):
    """Map any exception raised while fetching or parsing a source to ``(status, detail)``."""
    if isinstance(error, SourceError):
        return error.status, error.detail
    if isinstance(error, (socket.timeout, TimeoutError, cf.TimeoutError)):
        return TIMEOUT, "no answer in time"
    if isinstance(error, urllib.error.HTTPError):
        if error.code == 429:
            return BLOCKED, "rate limited (HTTP 429)"
        if error.code == 403:
            return BLOCKED, "refused (HTTP 403)"
        if error.code in (404, 410):
            return CHANGED, f"endpoint gone (HTTP {error.code})"
        if error.code >= 500:
            return OFFLINE, f"server error (HTTP {error.code})"
        return INVALID_RESPONSE, f"HTTP {error.code}"
    if isinstance(error, urllib.error.URLError):
        reason = error.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return TIMEOUT, "no answer in time"
        if isinstance(reason, socket.gaierror):
            return OFFLINE, "name lookup failed"
        return OFFLINE, str(reason)[:120]
    if isinstance(error, (ConnectionError, OSError)):
        return OFFLINE, str(error)[:120] or type(error).__name__
    if isinstance(error, (ValueError, ET.ParseError)):  # json.JSONDecodeError is a ValueError
        return INVALID_RESPONSE, f"{type(error).__name__}: {str(error)[:100]}"
    return PARSE_FAILURE, f"{type(error).__name__}: {str(error)[:100]}"


def magnet(info_hash, name):
    tr = "".join("&tr=" + urllib.parse.quote(t, safe="") for t in TRACKERS)
    return f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(name, safe='')}{tr}"


RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
_local = threading.local()  # final_url of the last get() and rows parsed before filtering, per thread


def get(url, accept=None, retries=1, ua=UA, timeout=None):
    """GET with the repo's retry rule: back off and retry transient statuses and network errors.

    An anti-bot challenge page (even one served with 403/503) raises ``SourceError(BLOCKED)``.
    """
    req = urllib.request.Request(url, headers={"User-Agent": ua, **({"Accept": accept} if accept else {})})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                _local.final_url = r.geturl()
                text = data.decode("utf-8", "replace")
            if is_challenge(text):
                raise SourceError(BLOCKED, "anti-bot challenge page")
            return text
        except urllib.error.HTTPError as e:
            if e.code in (403, 503):
                try:
                    if is_challenge(e.read().decode("utf-8", "replace")):
                        raise SourceError(BLOCKED, f"anti-bot challenge page (HTTP {e.code})") from e
                except OSError:
                    pass
            if e.code not in RETRY_STATUS or attempt == retries:
                raise
        except OSError:
            if attempt == retries:
                raise
        time.sleep(random.uniform(0, 0.5 * 2 ** attempt))


def get_json(url, timeout=None):
    text = get(url, "application/json", timeout=timeout)
    if text.lstrip().startswith("<"):
        raise SourceError(INVALID_RESPONSE, "HTML page where JSON was expected")
    return json.loads(text)


def parse_size(text):
    m = re.match(r"\s*([\d.,]+)\s*([KMGT]?)i?B", text or "", re.I)
    if not m:
        return 0
    mult = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}[m.group(2).upper()]
    return int(float(m.group(1).replace(",", "")) * mult)


def result(source, h, name, size, seeds, leech, mag=None):
    return {"source": source, "name": name, "size": size, "seeders": seeds,
            "leechers": leech, "magnet": mag or magnet(h, name)}


_good_host = {}  # source -> host that answered last, tried first next time


def hosts_for(source):
    hosts = list(SCHEMA[source]["hosts"])
    good = _good_host.get(source)
    return [good] + [h for h in hosts if h != good] if good in hosts else hosts


def _rank(status):
    """Which failure explains a source best when every mirror failed differently."""
    return (BLOCKED, CHANGED, INVALID_RESPONSE, TIMEOUT, OFFLINE, PARSE_FAILURE).index(status) \
        if status in (BLOCKED, CHANGED, INVALID_RESPONSE, TIMEOUT, OFFLINE, PARSE_FAILURE) else 99


def first_page(path, hosts=None, retries=1, source=None, looks_right=None):
    """Fetch *path* from the first host that answers with a genuine page; return ``(html, base_url)``.

    *looks_right(html)* rejects parked domains and unrelated pages, so the next mirror is tried instead.
    """
    hosts = hosts or hosts_for(source)
    timeout = SCHEMA.get(source, {}).get("timeout")
    failures = []
    for i, host in enumerate(hosts):
        try:
            text = get(f"https://{host}{path}", retries=retries if i == 0 else 0, ua=BROWSER_UA, timeout=timeout)
        except Exception as e:
            failures.append(classify_error(e) + (host,))
            continue
        if looks_right and not looks_right(text):
            final = getattr(_local, "final_url", "") or host
            failures.append((CHANGED, f"page from {final} lacks the expected structure", host))
            continue
        if source:
            _good_host[source] = host
        return text, f"https://{host}"
    status, detail, host = min(failures, key=lambda f: _rank(f[0])) if failures else (OFFLINE, "no hosts", "")
    raise SourceError(status, f"{host}: {detail}" if host else detail)


def _markers(source):
    marks = SCHEMA[source].get("markers") or []
    empty = SCHEMA[source].get("empty") or []
    return lambda page: any(m in page for m in marks + empty)


def check_rows(source, total, parsed):
    """Rows that look like results but none parsed means the row layout changed."""
    if total and not parsed:
        raise SourceError(CHANGED, f"{total} result rows found but none could be read (parser v{SCHEMA[source]['version']})")


# --- sources ---------------------------------------------------------------

def parse_yts(data):
    if not isinstance(data, dict) or "data" not in data:
        raise SourceError(CHANGED, "YTS answer has no 'data' object")
    out = []
    for mv in (data.get("data") or {}).get("movies") or []:
        base = mv.get("title_long") or mv.get("title") or "Unknown"
        for t in mv.get("torrents") or []:
            if not t.get("hash"):
                continue
            tag = " ".join(x for x in (t.get("quality"), t.get("type"), t.get("video_codec")) if x)
            name = f"{base} [{tag}]" if tag else base
            row = result("YTS", t["hash"].lower(), name, t.get("size_bytes") or 0,
                         t.get("seeds") or 0, t.get("peers") or 0)
            if t.get("date_uploaded_unix"):
                row["published"] = int(t["date_uploaded_unix"])
            out.append(row)
    return out


def yts(q):
    failures = []
    for host in hosts_for("YTS"):
        try:
            data = get_json(f"https://{host}/api/v2/list_movies.json?" +
                            urllib.parse.urlencode({"limit": 50, "query_term": q}), SCHEMA["YTS"]["timeout"])
        except Exception as e:
            failures.append(classify_error(e))
            continue
        _good_host["YTS"] = host
        return parse_yts(data)
    raise SourceError(*min(failures, key=lambda f: _rank(f[0])))


def parse_tpb(items):
    if not isinstance(items, list):
        raise SourceError(CHANGED, "apibay answer is not a list")
    out = []
    for it in items:
        h = (it.get("info_hash") or "").lower()
        if not h or set(h) == {"0"} or it.get("id") == "0":
            continue
        row = result("TPB", h, it.get("name") or "Unknown", int(it.get("size") or 0),
                     int(it.get("seeders") or 0), int(it.get("leechers") or 0))
        if str(it.get("added") or "").isdigit():
            row["published"] = int(it["added"])
        out.append(row)
    return out


def tpb(q):
    url = f"https://{hosts_for('TPB')[0]}/q.php?q=" + urllib.parse.quote(q)
    items = get_json(url, SCHEMA["TPB"]["timeout"])
    if isinstance(items, list) and len(items) == 1 and items[0].get("id") == "0":  # apibay's no-results sentinel
        items = get_json(url + "&cat=0", SCHEMA["TPB"]["timeout"])
    return parse_tpb(items)


X1337_HOSTS = tuple(SCHEMA["1337x"]["hosts"])
LIME_HOSTS = tuple(SCHEMA["LimeTorrents"]["hosts"])


def parse_1337x_listing(html_text):
    """Rows of a 1337x search page as ``(name, detail_path, seeds, leeches, size)``, most seeded first."""
    s = SCHEMA["1337x"]
    if not any(m in html_text for m in s["markers"]):
        raise SourceError(CHANGED, f"1337x search page structure not found (parser v{s['version']})")
    start = html_text.find("table-list")
    if start < 0:
        return []
    rows, total = [], 0
    for tr in re.split(r"<tr[\s>]", html_text[start:], flags=re.I)[1:]:
        if "/torrent/" not in tr:
            continue
        total += 1
        link = re.search(s["row_link"], tr, re.I)
        if not link:
            continue
        num = lambda pattern: int((re.search(pattern, tr, re.I) or [0, 0])[1])
        size = re.search(s["size"], tr, re.I)
        rows.append((html.unescape(link.group(2).strip()), link.group(1),
                     num(s["seeds"]), num(s["leeches"]), parse_size(size.group(1) if size else "")))
    check_rows("1337x", total, rows)
    rows.sort(key=lambda r: -r[2])
    return rows


def x1337_cat(q, cat):
    path = f"/search/{urllib.parse.quote(q).replace('%20', '+')}/1/"
    html_text, base = first_page(path, source="1337x", looks_right=_markers("1337x"))
    # The listing has no magnet; each needs its detail page. `keep` lets the caller drop junk first.
    return parse_1337x_listing(html_text), base


def x1337_details(rows, base, limit=12):
    pattern = SCHEMA["1337x"]["magnet"]

    def detail(row):
        name, path, s, l, size = row
        try:
            m = re.search(pattern, get(base + path, ua=BROWSER_UA, timeout=SCHEMA["1337x"]["timeout"]), re.I)
        except Exception:
            return None
        return result("1337x", "", name, size, s, l, html.unescape(m.group(0))) if m else None

    with cf.ThreadPoolExecutor(6) as ex:
        found = [r for r in ex.map(detail, rows[:limit]) if r]
    if rows and not found:
        raise SourceError(CHANGED, "detail pages carry no magnet link any more")
    return found


def x1337(q, keep=lambda name: True):
    """Listings for *q*; only rows passing ``keep(name)`` cost a detail-page request."""
    found, base = x1337_cat(q, None)
    _local.parsed = len(found)
    rows = [r for r in found if keep(r[0])]
    if not rows:
        return []
    rows.sort(key=lambda r: -r[2])
    return x1337_details(rows, base)


def parse_limetorrents(page, keep=lambda name: True):
    s = SCHEMA["LimeTorrents"]
    if any(e in page for e in s["empty"]) and not re.search(s["row"], page):
        return []
    if not any(m in page for m in s["markers"]):
        raise SourceError(CHANGED, f"LimeTorrents result table not found (parser v{s['version']})")
    out, total = [], 0
    n = lambda m: int(m.group(1).replace(",", "")) if m else 0
    for tr in re.split(r"<tr[\s>]", page, flags=re.I)[1:]:
        if "tt-name" not in tr or not re.search(s["row"], tr):
            continue
        total += 1
        link = re.search(s["hash"], tr)
        name = re.search(s["name"], tr, re.S)
        if not (link and name):
            continue
        title = html.unescape(name.group(1).strip())
        if not keep(title):
            out.append(None)  # parsed fine, just not wanted
            continue
        size = re.search(s["size"], tr)
        out.append(result("LimeTorrents", link.group(1).lower(), title, parse_size(size.group(1)) if size else 0,
                          n(re.search(s["seeds"], tr)), n(re.search(s["leech"], tr))))
    check_rows("LimeTorrents", total, out)
    _local.parsed = len(out)
    return [r for r in out if r]


def limetorrents(q, keep=lambda name: True):
    """LimeTorrents lists the info hash in its .torrent link, so no detail pages are needed."""
    slug = urllib.parse.quote(q.strip().replace(" ", "-"), safe="-")
    page, _ = first_page(f"/search/all/{slug}/seeds/1/", source="LimeTorrents", looks_right=_markers("LimeTorrents"))
    return parse_limetorrents(page, keep)


def parse_eztv(data):
    if not isinstance(data, dict):
        raise SourceError(CHANGED, "EZTV answer is not an object")
    out = []
    for t in data.get("torrents") or []:
        h = (t.get("hash") or "").lower()
        name = t.get("title") or t.get("filename") or ""
        if h and name:
            row = result("EZTV", h, name, int(t.get("size_bytes") or 0),
                         t.get("seeds") or 0, t.get("peers") or 0, t.get("magnet_url") or None)
            if t.get("date_released_unix"):
                row["published"] = int(t["date_released_unix"])
            out.append(row)
    return out


def eztv_by_imdb(imdb_id):
    """EZTV has no text search, but lists every episode of a show by IMDb id."""
    digits = re.sub(r"\D", "", imdb_id)
    out = []
    for page in (1, 2, 3):
        data = get_json(f"https://{hosts_for('EZTV')[0]}/api/get-torrents?imdb_id={digits}&limit=100&page={page}",
                        SCHEMA["EZTV"]["timeout"])
        batch = parse_eztv(data)
        out += batch
        if len(data.get("torrents") or []) < 100:
            break
    return out


def parse_nyaa(xml):
    ns = {"n": "https://nyaa.si/xmlns/nyaa"}
    root = ET.fromstring(xml)
    if root.find("channel") is None:
        raise SourceError(CHANGED, "Nyaa RSS has no channel")
    out = []
    for item in root.iter("item"):
        h = (item.findtext("n:infoHash", "", ns) or "").lower()
        name = item.findtext("title", "") or ""
        if not h or not name:
            continue
        out.append(result("Nyaa", h, name, parse_size(item.findtext("n:size", "", ns)),
                          int(item.findtext("n:seeders", "0", ns) or 0),
                          int(item.findtext("n:leechers", "0", ns) or 0)))
    return out


def nyaa(q):
    return parse_nyaa(get(f"https://{hosts_for('Nyaa')[0]}/?" + urllib.parse.urlencode({"page": "rss", "q": q, "c": "0_0", "f": "0"}),
                          timeout=SCHEMA["Nyaa"]["timeout"]))


def parse_subsplease(data):
    if isinstance(data, list) and not data:
        return []  # the API answers [] when nothing matched
    if not isinstance(data, dict):
        raise SourceError(CHANGED, "SubsPlease answer is not an object")
    out = []
    for e in data.values():
        dls = [d for d in e.get("downloads") or [] if d.get("magnet")]
        best = next((d for r in ("1080", "720", "480") for d in dls if d.get("res") == r), dls[0] if dls else None)
        if not best:
            continue
        ep = f" - {e['episode']}" if e.get("episode") else ""
        xl = re.search(r"[?&]xl=(\d+)", best["magnet"])
        row = result("SubsPlease", "", f"{e.get('show', 'Unknown')}{ep} [{best.get('res', '?')}p]",
                     int(xl.group(1)) if xl else 0, None, None, best["magnet"])  # no swarm figures published
        out.append(row)
    return out


def subsplease(q):
    return parse_subsplease(get_json(f"https://{hosts_for('SubsPlease')[0]}/api/?" +
                                     urllib.parse.urlencode({"f": "search", "s": q, "tz": "UTC"}),
                                     SCHEMA["SubsPlease"]["timeout"]))


SOURCES = [("YTS", yts), ("TPB", tpb), ("1337x", x1337), ("LimeTorrents", limetorrents),
           ("Nyaa", nyaa), ("SubsPlease", subsplease)]


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
                status, detail = classify_error(e)
                log(futs[f], f"{status} ({detail})")
    seen, unique = set(), []
    for r in sorted(results, key=lambda r: -(r["seeders"] or 0)):
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
        swarm = f"S:{r['seeders']} L:{r['leechers']}" if r["seeders"] is not None else "S:? L:?"
        print(f"{i:>3}. [{r['source']}] {r['name'][:70]}  {human(r['size'])}  {swarm}")
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
