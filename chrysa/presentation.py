"""What Browse shows, as plain data: the Qt views of both designs paint these, so they stay consistent and testable.

Only known facts are shown: a field the source did not publish, or a tag the release name does not carry, is left
out rather than guessed.
"""

from __future__ import annotations

import html
from pathlib import Path

from . import sources
from .ranking import Candidate

GOOD, WARN, BAD, INFO, MUTED = "good", "warn", "bad", "info", "muted"

_TONES = {  # (dark background, light background)
    GOOD: ("#3ecf8e", "#1f8f5a"),
    WARN: ("#f5b83d", "#a86b00"),
    BAD: ("#ff6b6b", "#c62828"),
}
STATE_TONE = {sources.HEALTHY: GOOD, sources.DEGRADED: WARN, sources.CHANGED: WARN, sources.OFFLINE: BAD,
              sources.DISABLED: MUTED, sources.UNKNOWN: MUTED}
_MARK = {GOOD: "✓", WARN: "⚠", BAD: "✕", INFO: "•", MUTED: "•"}


def tone_color(tone: str, dark: bool, theme=None) -> str:
    """A colour for *tone*; the theme's own SUCCESS/WARNING/ERROR win when it defines them."""
    own = {GOOD: "SUCCESS", WARN: "WARNING", BAD: "ERROR"}.get(tone)
    if theme is not None and own and getattr(theme, own, ""):
        return getattr(theme, own)
    if tone in _TONES:
        return _TONES[tone][0 if dark else 1]
    if theme is not None:
        return theme.TEXT_MUTED if tone == MUTED else theme.TEXT_SOFT or theme.TEXT
    return "#9a9a9a"


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def rate(bps: float) -> str:
    return f"{human_size(bps)}/s"


# --------------------------------------------------------------------------- result rows

SWARM_LABEL = {"healthy": ("Healthy", GOOD), "fair": ("Fair", WARN), "weak": ("Weak", WARN), "dead": ("No seeds", BAD)}
PERF_LABEL = {"fast": ("⚡ Fast", GOOD), "moderate": ("Moderate speed", INFO), "slow": ("Slow before", WARN)}


def spec_line(c: Candidate) -> str:
    """``Movie · 1080p • WEB-DL • H.264 • AAC 5.1`` — only the tags the release name actually carries."""
    kind = {"movie": "Movie", "season": "Season pack", "episode": "Episode"}.get(c.release.kind, "")
    tags = [t for t in (c.tags.resolution, c.tags.source, c.tags.hdr, c.tags.codec, c.tags.audio, c.tags.container) if t]
    return " • ".join(([kind] if kind else []) + tags)


def status_segments(c: Candidate) -> list[tuple[str, str]]:
    """The third row line as ``(text, tone)`` pieces: size, swarm health, performance, seeds, sources, warnings."""
    out: list[tuple[str, str]] = []
    if c.size:
        out.append((human_size(c.size), MUTED))
    if c.swarm in SWARM_LABEL:
        label, tone = SWARM_LABEL[c.swarm]
        out.append(("● " + label, tone))
    if c.performance in PERF_LABEL:
        out.append(PERF_LABEL[c.performance])
    if c.seeders is not None:
        out.append((f"{c.seeders} seeds / {c.leechers or 0} peers", MUTED))
    if c.evidence.verified_before:
        out.append(("✓ Verified before", GOOD))
    if c.warnings:
        out.append(("⚠ Suspicious name", BAD))
    names = c.sources[:2]
    more = len(c.sources) - len(names)
    out.append((" · ".join(names) + (f" +{more}" if more > 0 else ""), MUTED))
    return out


def detail_html(c: Candidate | None, dark: bool, theme=None, fallback_title: str = "", fallback_meta: str = "") -> str:
    """The panel beside the results: every known fact about the highlighted result and why it ranks where it does."""
    esc = html.escape
    muted = tone_color(MUTED, dark, theme)
    if c is None:
        if not fallback_title:
            return f"<p style='color:{muted}'>Highlight a result to see its details.</p>"
        return f"<h3>{esc(fallback_title)}</h3><p style='color:{muted}'>{esc(fallback_meta)}</p>"
    rows = [
        ("Release", c.name),
        ("Source", ", ".join(c.sources)),
        ("Size", human_size(c.size) if c.size else ""),
        ("Resolution", c.tags.resolution),
        ("Video", " ".join(t for t in (c.tags.source, c.tags.codec, c.tags.hdr) if t)),
        ("Audio", c.tags.audio),
        ("Container", c.tags.container),
        ("Group", c.tags.group),
        ("Seeders", "" if c.seeders is None else str(c.seeders)),
        ("Leechers", "" if c.leechers is None else str(c.leechers)),
        ("Swarm", SWARM_LABEL.get(c.swarm, ("", ""))[0]),
        ("Expected speed", PERF_LABEL.get(c.performance, ("", ""))[0].replace("⚡ ", "") or
         ("no history yet" if not c.evidence.samples else "")),
        ("Fallbacks", f"{len(c.alternatives)} equivalent release(s)" if c.alternatives else ""),
    ]
    table = "".join(
        f"<tr><td style='color:{muted};padding-right:12px'>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in rows if v
    )
    why = "".join(
        f"<div style='color:{tone_color(tone, dark, theme) if tone in _TONES else ''}'>{_MARK.get(tone, '•')} {esc(text)}</div>"
        for tone, text in c.reasons
    )
    score = c.scores
    breakdown = " · ".join(f"{k.replace('_', ' ')} {score[k]:.2f}" for k in
                           ("media_match", "quality", "safety", "availability", "performance", "source") if k in score)
    return (
        f"<h3 style='margin-bottom:2px'>{esc(c.display_title)}</h3>"
        f"<div style='color:{muted};margin-bottom:8px'>{esc(spec_line(c))}</div>"
        f"<table cellspacing='0' cellpadding='1'>{table}</table>"
        f"<p style='margin-top:10px;margin-bottom:4px'><b>WHY THIS RESULT</b></p>{why}"
        f"<p style='color:{muted};font-size:11px;margin-top:8px'>Score {score.get('overall', 0):.2f} — {esc(breakdown)}"
        f"{' · history weight ' + format(score['history_weight'], '.2f') if score.get('history_weight') else ''}</p>"
        f"<p style='color:{muted};margin-top:6px'>The torrent's file list is checked before downloading, and the "
        f"downloaded file is verified before it reaches the Library.</p>"
        f"<p style='color:{muted}'>[D] Download &nbsp; [Enter] Select</p>"
    )


# --------------------------------------------------------------------------- search status


def progress_line(expected: int, answered: dict[str, str]) -> str:
    """``Searching 7 sources…  ✓ 4  ⚠ 1  ✕ 1`` while sources report in."""
    ok = sum(1 for s in answered.values() if s in (sources.OK, "no results"))
    failed = sum(1 for s in answered.values() if s in ("offline", "timeout"))
    odd = len(answered) - ok - failed
    parts = [f"Searching {expected} sources…"]
    if ok:
        parts.append(f"✓ {ok}")
    if odd:
        parts.append(f"⚠ {odd}")
    if failed:
        parts.append(f"✕ {failed}")
    return "   ".join(parts)


def sources_button_text(health: dict[str, dict], outcomes: dict | None = None) -> tuple[str, str]:
    """``("Sources 5/7", tone)`` for the small status button."""
    rows = [r for r in health.values() if r.get("state") != sources.DISABLED]
    if not rows:
        return "Sources", MUTED
    good = sum(1 for r in rows if r.get("state") in (sources.HEALTHY, sources.DEGRADED))
    tone = GOOD if good == len(rows) and all(r.get("state") == sources.HEALTHY for r in rows) else \
        WARN if good else BAD
    return f"Sources {good}/{len(rows)}", tone


def source_entry(row: dict) -> tuple[str, str, str]:
    """``(label, tone, tooltip)`` for one line of the Sources menu."""
    state = row.get("state", sources.UNKNOWN)
    label = f"{row['source']}    {sources.STATE_LABEL.get(state, state)}"
    if row.get("last_result_count") and state not in (sources.OFFLINE, sources.DISABLED):
        label += f" · {row['last_result_count']} results"
    tips = []
    if row.get("last_error"):
        tips.append(row["last_error"])
    if row.get("response_ms"):
        tips.append(f"answered in {row['response_ms']} ms")
    if row.get("consecutive_failures"):
        tips.append(f"{row['consecutive_failures']} failure(s) in a row")
    if row.get("schema_version"):
        tips.append(f"parser v{row['schema_version']}")
    return label, STATE_TONE.get(state, MUTED), " · ".join(tips)


# --------------------------------------------------------------------------- downloads

HEALTH_LABEL = {"healthy": ("Healthy", GOOD), "slow": ("Slow", WARN), "stalled": ("Stalled", BAD),
                "connecting": ("Connecting", MUTED)}


def performance_label(bps: float) -> tuple[str, str]:
    if bps >= 3 << 20:
        return "⚡ Excellent", GOOD
    if bps >= 1 << 20:
        return "Good", GOOD
    if bps >= 200 << 10:
        return "Moderate", INFO
    return "Poor", WARN


def download_status(d) -> tuple[str, str]:
    """``(headline, tone)``: what the download is doing right now."""
    state = d.state
    if getattr(d, "kind", "") == "link":
        if state in ("queued", "downloading"):
            return (d.message, INFO if d.health == "recovering" else MUTED) if d.message else ("Downloading", INFO)
        if state in ("finished", "verified"):
            return "Download complete", GOOD
    if state == "downloading":
        if d.message and (d.health in ("stalled", "connecting") or not d.size_total):
            return d.message, HEALTH_LABEL.get(d.health, ("", MUTED))[1]
        return "Downloading", INFO
    if state == "finished":
        return "Download complete", GOOD
    if state == "validating":
        return d.message or "Checking contents…", INFO
    if state == "verified":
        return "✓ Verified media — added to Library", GOOD
    if state == "rejected":
        return "⚠ Validation failed — NOT added to the Library", BAD
    if state == "failed":
        return d.message or "Failed", BAD
    return d.message or state.capitalize(), MUTED


def download_stats(d) -> str:
    """``8.4 MB/s · ETA 06:42 · 3.3 / 4.2 GB · 17 peers • 21 seeds``."""
    if getattr(d, "kind", "") == "link" and d.state == "verified":
        return ", ".join(Path(p).name for p in d.admitted[:2])
    if d.state != "downloading" or not d.size_total:
        return ""
    parts = []
    if d.speed:
        parts.append(rate(d.speed))
    if d.eta and d.speed:
        parts.append(f"ETA {d.eta}")
    parts.append(f"{human_size(d.size_done)} / {human_size(d.size_total)}")
    swarm = []
    if d.peers:
        swarm.append(f"{d.peers} peers")
    if d.seeds is not None:
        swarm.append(f"{d.seeds} seeds")
    if swarm:
        parts.append(" • ".join(swarm))
    return " · ".join(parts)


def download_performance(d) -> tuple[str, str]:
    """``("Performance: ⚡ Excellent (avg 6.1 MB/s)", tone)`` once data flows."""
    if d.state != "downloading" or not d.avg_speed:
        return "", MUTED
    if d.health in ("slow", "stalled"):
        label, tone = HEALTH_LABEL[d.health]
    else:
        label, tone = performance_label(d.avg_speed)
    return f"Performance: {label} (avg {rate(d.avg_speed)}, peak {rate(d.peak_speed)})", tone


def checks_html(checks, dark: bool, theme=None) -> str:
    """The validation checklist, one line per check."""
    marks = {"pass": ("✓", GOOD), "fail": ("✕", BAD), "warn": ("⚠", WARN), "skip": ("–", MUTED)}
    lines = []
    for check in checks:
        mark, tone = marks.get(check.state, ("•", MUTED))
        detail = f" <span style='color:{tone_color(MUTED, dark, theme)}'>{html.escape(check.detail)}</span>" \
            if check.detail else ""
        lines.append(f"<span style='color:{tone_color(tone, dark, theme)}'>{mark}</span> {html.escape(check.label)}{detail}")
    return "<br>".join(lines)
