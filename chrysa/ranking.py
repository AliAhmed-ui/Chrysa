"""Understand, de-duplicate, score and explain Browse results.

Every raw source row becomes a :class:`Candidate`: what the release is (:func:`parse_tags` reads resolution, source,
codecs, audio and release group from the name, and only what is actually written there), how safe the name looks,
and a set of scores. The final order is *not* "most seeders first": advertised seeders only estimate availability,
and once Chrysa has downloaded similar torrents (same torrent, release group or source) the measured speed takes
over more and more of the ranking (see :class:`chrysa.history.Evidence`).

Everything here is a pure function of the rows, the query and the history, so it is fast and testable offline.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field

from . import releases
from .history import Evidence
from .releases import EPISODE, MOVIE, OTHER, SEASON

# --------------------------------------------------------------------------- release tags

_W = r"(?<![a-z0-9])"
_E = r"(?![a-z0-9])"
_SOURCES = [  # (label, pattern, quality weight); first match wins
    ("Remux", rf"(?i){_W}(?:bd)?remux{_E}", 1.0),
    ("BluRay", rf"(?i){_W}(?:blu-?ray|bdrip|brrip|bd25|bd50|bdmv){_E}", 1.0),
    ("WEB-DL", rf"(?i){_W}(?:web-?dl|webdl|amzn|nf|dsnp|hmax|atvp){_E}", 0.95),
    ("WEBRip", rf"(?i){_W}web-?rip{_E}", 0.85),
    ("WEB", rf"(?i){_W}web{_E}", 0.9),
    ("HDTV", rf"(?i){_W}(?:hdtv|pdtv){_E}", 0.7),
    ("DVDRip", rf"(?i){_W}(?:dvdrip|dvd-?r|dvd5|dvd9){_E}", 0.55),
    ("HDRip", rf"(?i){_W}hdrip{_E}", 0.6),
    ("CAM", rf"(?i){_W}(?:cam|camrip|hdcam|hd-?ts|telesync|telecine|hd-?tc|dvdscr|screener|tsrip){_E}", 0.05),
]
_CAM_SHORT = re.compile(rf"(?i){_W}(?:ts|tc){_E}")  # bare TS/TC only counts next to other cam hints
_CODECS = [("AV1", r"(?i)\bav1\b"), ("HEVC", r"(?i)(?:x\.?265|h\.?265|hevc)"), ("H.264", r"(?i)(?:x\.?264|h\.?264|\bavc\b)"),
           ("XviD", r"(?i)\bxvid\b"), ("DivX", r"(?i)\bdivx\b"), ("VP9", r"(?i)\bvp9\b")]
_AUDIO = [("Atmos", r"(?i)\batmos\b"), ("TrueHD", r"(?i)\btruehd\b"), ("DTS-HD", r"(?i)\bdts-?hd(?:[\s._-]?ma)?\b"),
          ("DTS", r"(?i)\bdts\b"), ("EAC3", r"(?i)(?:\be-?ac-?3\b|\bddp|\bdd\+|\bdd[\s._]?p\d)"),
          ("AC3", r"(?i)(?:\bac-?3\b|\bdd[\s._]?[25]\.?[01]\b|\bdolby[\s._]digital\b)"),
          ("AAC", r"(?i)\baac(?:[\s._-]?(?:lc|2\.0|5\.1))?\b"), ("FLAC", r"(?i)\bflac\b"), ("Opus", r"(?i)\bopus\b"),
          ("MP3", r"(?i)\bmp3\b")]
_CHANNELS = re.compile(r"(?<![\d])([7652])[\s._]?([01])(?![\d])(?=[^p\d]|$)")
_HDR = [("Dolby Vision", r"(?i)(?:\bdv\b|\bdovi\b|dolby[\s._-]?vision)"), ("HDR10+", r"(?i)\bhdr10(?:\+|plus)"),
        ("HDR10", r"(?i)\bhdr10\b"), ("HDR", r"(?i)\bhdr\b"), ("SDR", r"(?i)\bsdr\b")]
_CONTAINER = re.compile(r"(?i)\.(mkv|mp4|avi|m4v|webm|ts|wmv)$")
_GROUP = re.compile(r"-\s?([A-Za-z0-9][A-Za-z0-9._]{1,19}?)(?:\.(?:mkv|mp4|avi|m4v))?$")
_BRACKET_GROUP = re.compile(r"^\[([^\]]{2,24})\]")  # "[SubsPlease] Show - 01"


@dataclass(frozen=True)
class Tags:
    resolution: str = ""  # 2160p / 1080p / 720p / 576p / 480p
    source: str = ""  # Remux / BluRay / WEB-DL / WEBRip / WEB / HDTV / DVDRip / HDRip / CAM
    codec: str = ""
    audio: str = ""  # codec plus channels when written, e.g. "EAC3 5.1"
    hdr: str = ""
    container: str = ""
    group: str = ""

    def known(self) -> list[str]:
        return [v for v in (self.resolution, self.source, self.hdr, self.codec, self.audio) if v]


def parse_tags(name: str, source_name: str = "") -> Tags:
    """Read the technical tags written in a release name. Unknown stays ``""``; nothing is guessed."""
    text = re.sub(r"[_]+", " ", name or "")
    resolution = releases._quality(text)
    src = next((label for label, pattern, _ in _SOURCES if re.search(pattern, text)), "")
    if not src and _CAM_SHORT.search(text) and re.search(r"(?i)\b(?:cam|hdcam|telesync|new)\b", text):
        src = "CAM"
    codec = next((label for label, pattern in _CODECS if re.search(pattern, text)), "")
    audio = next((label for label, pattern in _AUDIO if re.search(pattern, text)), "")
    channels = _CHANNELS.search(re.sub(r"(?i)\b(?:h|x)\.?26[45]|\d{3,4}p", " ", text))
    if audio and channels:
        audio = f"{audio} {channels.group(1)}.{channels.group(2)}"
    hdr = next((label for label, pattern in _HDR if re.search(pattern, text)), "")
    ext = _CONTAINER.search(text)
    container = ext.group(1).upper() if ext else ""
    if not container and source_name == "YTS":
        container = "MP4"  # YTS publishes MP4 files only
    group = ""
    if source_name == "YTS":
        group = "YTS"
    elif (bracket := _BRACKET_GROUP.match(text.strip())):
        group = bracket.group(1).strip()
    else:
        stripped = re.sub(r"\s*\[[^\]]*\]\s*$", "", text.strip())  # "... -KILLERS [eztv]"
        match = _GROUP.search(stripped)
        if match and not re.fullmatch(r"(?i)\d+|dl|rip|web|ray|hd|dts|ma|aac|x26[45]|h26[45]", match.group(1)):
            group = match.group(1)
    return Tags(resolution, src, codec, audio, hdr, container, group)


# --------------------------------------------------------------------------- intent

INTENT_MOVIE, INTENT_SERIES, INTENT_SEASON, INTENT_EPISODE, INTENT_DOC, INTENT_ANY = (
    "movie", "series", "season", "episode", "documentary", "any")
_DOC = re.compile(r"(?i)\b(?:documentary|docu|planet earth|blue planet|bbc earth|nat ?geo|national geographic|frozen planet|"
                  r"our planet|cosmos)\b")


def detect_intent(query: releases.Query, targets: list[releases.Title]) -> str:
    """What the user most likely wants, from the query and the IMDb matches. ``any`` when unsure."""
    if query.episode is not None:
        return INTENT_EPISODE
    if query.season is not None:
        return INTENT_SEASON
    if _DOC.search(query.raw):
        return INTENT_DOC
    if targets:
        first = targets[0]
        if all(t.is_series == first.is_series for t in targets[:2]):
            return INTENT_SERIES if first.is_series else INTENT_MOVIE
    return INTENT_ANY


def _kind_fit(intent: str, release: releases.Release) -> float:
    """1.0 = exactly the kind of thing asked for. A season pack answers an episode search, but less well."""
    kind = release.kind
    if intent in (INTENT_ANY, INTENT_DOC):
        return 0.9
    multi = kind == SEASON and (release.season is None or (release.last_season or release.season) != release.season)
    table = {
        INTENT_MOVIE: {MOVIE: 1.0},
        INTENT_SERIES: {SEASON: 1.0, EPISODE: 0.9},
        INTENT_SEASON: {SEASON: 0.9 if multi else 1.0, EPISODE: 0.75},
        INTENT_EPISODE: {EPISODE: 1.0, SEASON: 0.55 if multi else 0.65},
    }[intent]
    if kind in table:
        return table[kind]
    return 0.8 if kind == OTHER else 0.65


# --------------------------------------------------------------------------- safety

_SUSPICIOUS = [
    (r"(?i)\bpassword(?:ed)?\b|\bpasswd\b", "mentions a password"),
    (r"(?i)\.(?:exe|scr|bat|cmd|msi|com|vbs|js|jar|ps1|apk|lnk)\b", "names an executable file"),
    (r"(?i)\.(?:rar|zip|7z)\b|\b(?:rar|zip)\s*archive\b", "is packed in an archive"),
    (r"(?i)\bcodec[\s._-]*(?:pack|required|needed)|\b(?:special|required)[\s._-]*player\b", "asks for a special codec/player"),
    (r"(?i)\b(?:setup|installer|install)\b", "mentions an installer"),
    (r"(?i)\b(?:free[\s._-]*download|direct[\s._-]*download|download[\s._-]*now|click[\s._-]*here)\b", "reads like spam"),
    (r"(?i)\.(?:mkv|mp4|avi)\.(?:exe|scr|bat|lnk|zip|rar)\b", "has a double extension"),
]
_SOFTWARE = re.compile(
    r"(?i)(?<![a-z0-9])(?:windows|win(?:32|64)|macos|mac[\s._-]?os|x64|x86|portable|multilingual|apk|iso|repack[\s._-]+by|"
    r"game|gog|steam|plugin|vst|adobe|office|setup|v\d+\.\d+(?:\.\d+)+)(?![a-z0-9])"
)
_EXTRA = re.compile(r"(?i)\b(?:sample|trailer|teaser|featurette|behind[\s._-]the[\s._-]scenes)\b")

_MIN_SIZE = {MOVIE: 300 << 20, EPISODE: 40 << 20, SEASON: 300 << 20}  # below this a release is implausible
_MAX_SIZE = 200 << 30


def safety_signals(name: str, kind: str, size: int, tags: Tags) -> list[str]:
    """Reasons a result looks unsafe or fake; empty when nothing stands out."""
    found = [why for pattern, why in _SUSPICIOUS if re.search(pattern, name)]
    software = _SOFTWARE.findall(name)
    if software and len(software) + (0 if tags.known() else 1) >= 2:
        found.append("looks like software, not a video")
    if _EXTRA.search(name):
        found.append("is a sample/trailer, not the full video")
    if size and kind in _MIN_SIZE and size < _MIN_SIZE[kind] and tags.resolution in ("1080p", "2160p"):
        found.append("is far too small for its claimed quality")
    if size > _MAX_SIZE:
        found.append("is implausibly large")
    return found


# --------------------------------------------------------------------------- candidates

_RES_PREF = {"2160p": 0.85, "1080p": 1.0, "720p": 0.78, "576p": 0.5, "480p": 0.45}
_SRC_PREF = {label: weight for label, _, weight in _SOURCES}


@dataclass
class Candidate:
    """One release as Browse shows it, possibly found on several sources."""

    name: str  # the release name as published
    target: str  # magnet link or .torrent URL
    info_hash: str
    sources: list[str]
    size: int
    seeders: int | None
    leechers: int | None
    release: releases.Release
    tags: Tags
    relevance: int
    published: int | None = None
    alternatives: list[str] = field(default_factory=list)  # equivalent releases (other hashes), for fallback
    scores: dict[str, float] = field(default_factory=dict)
    reasons: list[tuple[str, str]] = field(default_factory=list)  # (tone: good|warn|bad|info, text)
    warnings: list[str] = field(default_factory=list)
    swarm: str = ""  # healthy | fair | weak | dead | "" (not published)
    performance: str = ""  # fast | moderate | slow | "" (no history)
    evidence: Evidence = field(default_factory=Evidence)

    @property
    def source(self) -> str:
        return self.sources[0] if self.sources else ""

    @property
    def overall(self) -> float:
        return self.scores.get("overall", 0.0)

    @property
    def display_title(self) -> str:
        return display_title(self.name, self.release)

    @property
    def high_quality(self) -> bool:
        return self.overall >= 0.62 and self.tags.resolution in ("720p", "1080p", "2160p") and self.tags.source != "CAM"

    def meta(self) -> dict:
        """What the download history keeps about this candidate."""
        return {"info_hash": self.info_hash, "source": self.source, "title": self.name, "size": self.size,
                "resolution": self.tags.resolution, "release_group": self.tags.group,
                "seeders": self.seeders, "leechers": self.leechers}


def display_title(name: str, release: releases.Release | None) -> str:
    """``The.Incredible.Hulk.2008.1080p.BluRay`` → ``The Incredible Hulk (2008)``; episodes keep ``S02E03``."""
    text = re.sub(r"^\s*\[[^\]]*\]\s*", "", name)  # leading [Group]
    text = re.sub(r"^\s*www\.\S+\s*[-–]?\s*", "", text)
    text = re.sub(r"[._]+", " ", text)
    cut = re.search(
        r"(?i)(?<![a-z0-9])(?:(?:19|20)\d{2}(?![\d])|s\d{1,2}(?:e\d{1,3})?(?![a-z0-9])|\d{1,2}x\d{2,3}|season\s*\d|complete|"
        r"480p|576p|720p|1080p|2160p|4k|uhd|bluray|web|hdtv|dvdrip|x26[45]|h\s?26[45]|hevc|\[|\()", text)
    head = text[:cut.start()] if cut and cut.start() > 0 else text
    head = re.sub(r"\s+-\s*$|\s+", " ", head).strip(" -–")
    if not head:
        return name
    if release is None:
        return head
    if release.kind == MOVIE and release.year:
        return f"{head} ({release.year})"
    if release.kind == EPISODE and release.season is not None:
        return f"{head} · S{release.season:02d}E{release.episode:02d}"
    if release.kind == SEASON:
        if release.season is None:
            return f"{head} · Complete series"
        if release.last_season and release.last_season != release.season:
            return f"{head} · Seasons {release.season}–{release.last_season}"
        return f"{head} · Season {release.season}"
    return head


def merge_duplicates(rows: list[dict]) -> list[dict]:
    """One row per torrent: same info hash = same torrent, whichever site listed it.

    The merged row keeps every source, the highest seeder/leecher figures (sites refresh at different times), the
    most descriptive name and a known size.
    """
    from .sources import info_hash

    merged: dict[str, dict] = {}
    for row in rows:
        key = info_hash(row["magnet"]) or row["magnet"]
        known = merged.get(key)
        if known is None:
            merged[key] = {**row, "sources": [row["source"]], "info_hash": info_hash(row["magnet"])}
            continue
        if row["source"] not in known["sources"]:
            known["sources"].append(row["source"])
        for k in ("seeders", "leechers"):
            if row.get(k) is not None:
                known[k] = max(known[k] or 0, row[k])
        if len(parse_tags(row["name"]).known()) > len(parse_tags(known["name"]).known()):
            known["name"] = row["name"]
        known["size"] = known["size"] or row["size"]
        known["published"] = known.get("published") or row.get("published")
    return list(merged.values())


def _equivalent(a: Candidate, b: Candidate) -> bool:
    """Re-uploads of one release: the same normalised name and sizes within 1 % of each other."""
    if releases.normalise(a.name) != releases.normalise(b.name):
        return False
    if not a.size or not b.size:
        return a.size == b.size
    return abs(a.size - b.size) <= 0.01 * max(a.size, b.size)


# --------------------------------------------------------------------------- scoring

SPEED_FAST, SPEED_OK = 3 << 20, 500 << 10  # bytes/s


def score(c: Candidate, intent: str, evidence: Evidence, source_states: dict[str, str],
          source_reliability: dict[str, float], now: float | None = None) -> None:
    """Fill ``c.scores``, ``c.reasons``, ``c.swarm`` and ``c.performance``."""
    now = now or time.time()
    reasons: list[tuple[str, str]] = []
    c.evidence = evidence

    # media match: how well the title matches, and whether it is the kind of thing asked for
    fit = _kind_fit(intent, c.release)
    match = min(1.0, c.relevance / 5) * fit
    if c.relevance >= 4 and fit >= 1.0:
        reasons.append(("good", "Strong media match"))
    elif fit < 1.0 and intent == INTENT_EPISODE and c.release.kind == SEASON:
        reasons.append(("info", "Season pack that includes the episode (larger download)"))
    elif c.relevance >= 3:
        reasons.append(("good", "Title matches the search"))
    else:
        reasons.append(("info", "Partial title match"))
    if fit < 0.7:
        reasons.append(("warn", f"A {c.release.kind}, but the search looks like a {intent}"))

    # quality
    res = _RES_PREF.get(c.tags.resolution, 0.5)
    src = _SRC_PREF.get(c.tags.source, 0.65)
    quality = 0.55 * res + 0.45 * src
    if c.tags.source == "CAM":
        quality = min(quality, 0.1)
        reasons.append(("bad", "Camera recording (CAM/TS): poor picture and sound"))
    elif c.tags.resolution or c.tags.source:
        reasons.append(("good" if quality >= 0.8 else "info", " ".join(filter(None, (c.tags.resolution, c.tags.source)))))
    if releases.is_foreign(c.name):
        quality *= 0.7
        reasons.append(("info", "Labelled as another language"))

    # safety (name level; the file list and the file itself are checked later)
    c.warnings = safety_signals(c.name, c.release.kind, c.size, c.tags)
    safety = max(0.0, 1.0 - 0.4 * len(c.warnings))
    if any("double extension" in w or "executable" in w for w in c.warnings):
        safety = 0.0
    for warning in c.warnings:
        reasons.append(("bad", f"Name {warning}" if not warning.startswith(("looks", "is", "has")) else f"Release {warning}"))
    if not c.warnings:
        reasons.append(("good", "No suspicious signs in the release name"))

    # source standing
    states = [source_states.get(s, "unknown") for s in c.sources]
    standing = max({"healthy": 1.0, "unknown": 0.8, "degraded": 0.65, "changed": 0.5}.get(s, 0.5) for s in states)
    reliability = max((source_reliability.get(s, 0.67) for s in c.sources), default=0.67)
    source_score = 0.6 * standing + 0.4 * reliability
    if len(c.sources) > 1:
        reasons.append(("good", f"Listed by {len(c.sources)} sources"))

    # availability from the advertised swarm
    if c.seeders is None:
        availability, c.swarm = 0.45, ""
    else:
        s, l = c.seeders, c.leechers or 0
        availability = min(1.0, math.log1p(s) / math.log1p(150))
        if s == 0:
            availability, c.swarm = 0.02, "dead"
            reasons.append(("bad", "No seeders reported"))
        elif s >= 10:
            c.swarm = "healthy"
            reasons.append(("good", f"Healthy swarm ({s} seeds, {l} peers)"))
        elif s >= 3:
            c.swarm = "fair"
            reasons.append(("info", f"Modest swarm ({s} seeds, {l} peers)"))
        else:
            c.swarm = "weak"
            reasons.append(("warn", f"Very few seeds ({s})"))
        if s and l > 4 * s:
            availability *= 0.85  # many downloaders sharing few seeds
        if c.published and now - c.published > 3 * 365 * 86400 and s < 5:
            availability *= 0.8  # old and thin: the advertised figure is probably stale

    # measured performance from history; its weight grows with the evidence
    performance = 0.0
    if evidence.samples:
        speed = evidence.speed
        performance = min(1.0, max(0.0, math.log10(max(speed, 1) / 30_000) / math.log10(10_000_000 / 30_000)))
        performance *= 0.4 + 0.6 * evidence.success
        where = {"torrent": "this torrent", "group": f"{c.tags.group} releases", "source": f"{c.source}"}[evidence.scope]
        if evidence.confidence >= 0.2:
            if speed >= SPEED_FAST and evidence.success >= 0.6:
                c.performance = "fast"
                reasons.append(("good", f"Fast in past downloads of {where} (~{_rate(speed)})"))
            elif speed >= SPEED_OK:
                c.performance = "moderate"
                reasons.append(("info", f"Moderate speed in past downloads of {where} (~{_rate(speed)})"))
            else:
                c.performance = "slow"
                reasons.append(("warn", f"Slow in past downloads of {where} (~{_rate(speed)})"))
        if evidence.success < 0.5:
            reasons.append(("warn", f"Past downloads of {where} often failed or stalled"))
    if evidence.verified_before:
        reasons.append(("good", "Verified as real media when downloaded before"))
    weight = evidence.confidence
    delivery = (1 - weight) * availability + weight * performance

    known = [c.tags.resolution, c.tags.source, c.tags.codec, c.tags.audio, c.tags.group, c.size > 0, c.seeders is not None]
    metadata_confidence = sum(bool(k) for k in known) / len(known)

    overall = (0.28 * match + 0.14 * quality + 0.14 * safety + 0.07 * source_score + 0.32 * delivery
               + 0.05 * metadata_confidence)
    if safety < 0.35:
        overall *= 0.3
    c.scores = {
        "media_match": round(match, 3), "quality": round(quality, 3), "safety": round(safety, 3),
        "source": round(source_score, 3), "availability": round(availability, 3),
        "performance": round(performance, 3), "metadata_confidence": round(metadata_confidence, 3),
        "history_weight": round(weight, 3), "overall": round(overall, 4),
    }
    c.reasons = reasons


def _rate(bps: float) -> str:
    return f"{bps / (1 << 20):.1f} MB/s" if bps >= 1 << 20 else f"{bps / 1024:.0f} KB/s"


@dataclass
class Ranked:
    candidates: list[Candidate]
    raw_rows: int = 0
    unique: int = 0
    unrelated: int = 0
    unsafe: int = 0


def rank(rows: list[dict], query: releases.Query, targets: list[releases.Title], history, source_states: dict[str, str],
         now: float | None = None) -> Ranked:
    """Rows from every source → relevant, de-duplicated, scored candidates, best first."""
    from .sources import info_hash

    intent = detect_intent(query, targets)
    reliability = history.source_reliability()
    unrelated = unsafe = 0
    candidates = []
    for row in merge_duplicates(rows):
        release = releases.classify(row["name"])
        relevance = releases.relevance(release, query, targets) if release else 0
        if release is None or not relevance:
            unrelated += 1
            continue
        tags = parse_tags(row["name"], row["sources"][0])
        c = Candidate(row["name"], row["magnet"], row.get("info_hash") or info_hash(row["magnet"]), row["sources"],
                      row["size"] or 0, row.get("seeders"), row.get("leechers"), release, tags, relevance,
                      row.get("published"))
        evidence = history.evidence(c.info_hash, c.source, tags.group)
        score(c, intent, evidence, source_states, reliability, now)
        if c.scores["safety"] < 0.35:
            unsafe += 1
            continue
        candidates.append(c)

    # Re-uploads of the same release collapse into one row; the others become its fallbacks.
    groups: list[list[Candidate]] = []
    for c in candidates:
        home = next((g for g in groups if _equivalent(g[0], c)), None)
        if home is None:
            groups.append([c])
        else:
            home.append(c)
    shown = []
    for members in groups:
        members.sort(key=lambda c: c.overall, reverse=True)
        best = members[0]
        best.alternatives = [m.target for m in members[1:]]
        for m in members[1:]:
            best.sources += [s for s in m.sources if s not in best.sources]
        shown.append(best)
    shown.sort(key=lambda c: (c.overall, c.seeders or 0), reverse=True)

    # Fallbacks also include the next-best different releases of the same thing (same kind, episode and resolution).
    for c in shown:
        for other in shown:
            if len(c.alternatives) >= 3:
                break
            if other is not c and other.target not in c.alternatives and _same_media(c, other) and other.scores["safety"] >= 0.6:
                c.alternatives.append(other.target)
    return Ranked(shown, len(rows), len(shown), unrelated, unsafe)


def _same_media(a: Candidate, b: Candidate) -> bool:
    ra, rb = a.release, b.release
    return (ra.kind == rb.kind and ra.title == rb.title and ra.season == rb.season and ra.episode == rb.episode
            and (ra.year == rb.year or not ra.year or not rb.year) and a.tags.resolution == b.tags.resolution
            and a.tags.source != "CAM" and b.tags.source != "CAM")
