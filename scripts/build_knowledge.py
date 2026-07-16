#!/usr/bin/env python3
"""Distill data/<site>/site_index.json → data/<site>/knowledge.md (+ details).

Turns the raw crawl artifact (pages + captured JSON feeds) into a compact
markdown digest a voice persona can answer questions from. The digest is
injected into the agent's system prompt (see src/agent/knowledge.ts), so it
is written dense and deterministic: stable sections first / volatile last
(cache-friendly), dates normalized, and rollups (next launch, counts)
precomputed here so the LLM never does date math or counting.

Also written: data/<site>/knowledge/<feed>.md — fuller per-feed detail files
(~1-2k tokens each) that are safe to read into a conversation. The raw
site_index.json (hundreds of KB) is builder input only, never LLM input.

An optional hand-authored overview at docs/knowledge/<site>.md is included
verbatim when present — crawls of JS-rendered SPAs capture feeds, not page
content, so authored context is what closes that gap.

Fail-loud contract for cron: if the index is missing/empty or the digest
comes out suspiciously small or over token budget, the previous knowledge.md
is left untouched and the exit code is nonzero.

Usage:
    python3 scripts/build_knowledge.py [site ...]   # default: every site in data/
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OVERVIEW_DIR = ROOT / "docs" / "knowledge"

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
LOCAL_LABEL = "Pacific"

# Digest size guards (chars ≈ tokens*4). Over budget → fail, keep last good.
MIN_CHARS = 2_000
WARN_CHARS = 45_000
MAX_CHARS = 60_000

FLOWN_STATUSES = {"Success", "Failure", "Partial Failure"}
VAGUE_PRECISIONS = {"Month", "Quarter", "Half", "Year", "Fiscal Year", "Decade"}

warnings: list[str] = []


def warn(msg: str) -> None:
    warnings.append(msg)
    print(f"  WARN: {msg}", file=sys.stderr)


def write_atomic(path: Path, text: str) -> None:
    """tmp + rename: the running server hot-reloads these files on mtime
    change, and an in-place truncate-then-write could hand it a partial
    digest mid-turn."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def feed_has_content(name: str, feed: object) -> bool:
    """Whether a captured feed carries any substantive data (used to refuse
    replacing a good digest with one built from a degraded crawl)."""
    if not isinstance(feed, dict) or "_error" in feed:
        return False
    keys = {
        "launches.json": "results",
        "ufo-cases.json": "cases",
        "ufo-wire.json": "items",
        "data-lens-articles.json": "articles",
        "becker-tour.json": "events",
        "maxq-podcast.json": "episodes",
        "ufo-podcast.json": "episodes",
        "dsn-snapshot.json": "stations",
    }
    key = keys.get(name)
    if key is not None:
        return bool(feed.get(key))
    return any(v for v in feed.values() if isinstance(v, (list, dict, str, int, float)))


def clean(text: str, limit: int = 220) -> str:
    """One-line, whitespace-collapsed, truncated at a word boundary."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_utc(value: str | None, day_only: bool = False) -> str:
    dt = parse_dt(value)
    if not dt:
        return value or "date unknown"
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d") if day_only else dt.strftime("%Y-%m-%d %H:%M UTC")


def fmt_launch_time(net: str | None, precision: str) -> str:
    """Render a launch NET honestly per its precision, in UTC + local."""
    dt = parse_dt(net)
    if not dt:
        return "date TBD"
    dt = dt.astimezone(timezone.utc)
    if precision in VAGUE_PRECISIONS:
        return f"NET {dt.strftime('%B %Y')} ({precision.lower()} precision, date not set)"
    if precision == "Day":
        return f"{dt.strftime('%Y-%m-%d')} (time TBD)"
    local = dt.astimezone(LOCAL_TZ)
    return f"{dt.strftime('%Y-%m-%d %H:%M UTC')} ({local.strftime('%b %d %I:%M %p')} {LOCAL_LABEL})"


def feed_captured(feed: dict) -> str | None:
    """Prefer the feed's own timestamp over crawl time — sites serve stale data."""
    ts = feed.get("fetchedAt") or feed.get("generated_at")
    if isinstance(ts, str):
        return fmt_utc(ts)
    return None


def section_header(title: str, feed: dict, note: str = "") -> list[str]:
    lines = [f"## {title}"]
    meta = []
    captured = feed_captured(feed)
    if captured:
        meta.append(f"Data captured {captured}.")
    if note:
        meta.append(note)
    if meta:
        lines.append(" ".join(meta))
    return lines


# ── Feed renderers ──────────────────────────────────────────────
# Each takes (feed_json, crawled_at, char_limit) and returns markdown lines.
# char_limit lets the same renderer emit the digest (tight) and detail files.


def render_launches(feed: dict, crawled: datetime | None, limit: int) -> list[str]:
    lines = section_header(
        "Rocket launches (Launch Command / Mission Tracker)",
        feed,
        "Launch schedules change frequently; treat exact times as of the snapshot.",
    )
    results = sorted(feed.get("results", []), key=lambda r: r.get("net") or "")
    if not results:
        warn("launches feed has no results")
        lines.append("EMPTY in this snapshot — no launch data was captured.")
        return lines

    upcoming: list[dict] = []
    recent: list[dict] = []
    stale: list[dict] = []
    for r in results:
        abbrev = (r.get("status") or {}).get("abbrev", "")
        net = parse_dt(r.get("net"))
        if abbrev in FLOWN_STATUSES:
            recent.append(r)
        elif net and crawled and net < crawled:
            # NET passed but status never confirmed flown — scrubs and holds
            # are common; claiming these "already launched" would be wrong.
            stale.append(r)
        else:
            upcoming.append(r)

    def line(r: dict) -> str:
        status = (r.get("status") or {}).get("name", "Unknown status")
        provider = (r.get("launch_service_provider") or {}).get("name", "")
        mission = r.get("mission") or {}
        orbit = ((mission.get("orbit") or {}).get("name")) or ""
        where = ((r.get("pad") or {}).get("location") or {}).get("name", "")
        precision = ((r.get("net_precision") or {}).get("name")) or ""
        when = fmt_launch_time(r.get("net"), precision)
        detail = ", ".join(x for x in [provider, orbit, where] if x)
        out = f"- {when} — {r.get('name', 'Unnamed launch')} [{status}]"
        if detail:
            out += f" ({detail})"
        desc = clean(mission.get("description", ""), max(120, limit - 60))
        if desc:
            out += f" — {desc}"
        return out

    # Deterministic rollups so the model never does date math or counting.
    firm = [
        r
        for r in upcoming
        if ((r.get("net_precision") or {}).get("name") or "") not in VAGUE_PRECISIONS
        and parse_dt(r.get("net"))
    ]
    if firm:
        nxt = firm[0]
        lines.append(f"Next scheduled launch: {line(nxt)[2:]}")
    providers: dict[str, int] = {}
    for r in upcoming:
        name = (r.get("launch_service_provider") or {}).get("name", "Unknown")
        providers[name] = providers.get(name, 0) + 1
    if providers:
        tally = ", ".join(f"{k} {v}" for k, v in sorted(providers.items(), key=lambda x: -x[1]))
        lines.append(f"Upcoming launches in this snapshot: {len(upcoming)} ({tally}).")

    if recent:
        lines.append("### Recently flown (already launched)")
        lines.extend(line(r) for r in recent)
    if stale:
        lines.append(
            "### Listed NET has passed, outcome not confirmed in this snapshot "
            "(may have launched, scrubbed, or slipped — do not state these flew)"
        )
        lines.extend(line(r) for r in stale)
    lines.append("### Upcoming")
    lines.extend(line(r) for r in upcoming)
    return lines


def render_ufo_cases(feed: dict, crawled: datetime | None, limit: int) -> list[str]:
    lines = section_header("UFO debate cases (Anomaly Division / UFO Files)", feed)
    cases = feed.get("cases", [])
    if not cases:
        warn("ufo-cases feed is empty")
        lines.append("EMPTY in this snapshot.")
        return lines
    for c in cases:
        cc = c.get("consensus_counts") or {}
        consensus = ", ".join(f"{k} {v}" for k, v in cc.items()) or "no consensus data"
        lines.append(
            f"- Debate {c.get('debate_no', '?')}: {c.get('title', 'Untitled')}"
            f" ({c.get('era', '?')}) — {clean(c.get('one_line', ''), limit)}"
            f" Prior status: {c.get('case_status_before', 'unknown')}."
            f" Debated by {c.get('persona_count', '?')} personas; consensus tallies: {consensus}."
        )
    return lines


def render_ufo_wire(feed: dict, crawled: datetime | None, limit: int) -> list[str]:
    label = feed.get("dateLabel", "")
    lines = section_header(
        f"UFO & UAP news wire{f' — edition dated {label}' if label else ''}",
        feed,
        "Updated daily on the site.",
    )
    items = feed.get("items", [])
    if not items:
        warn("ufo-wire feed is empty")
        lines.append("EMPTY in this snapshot.")
        return lines
    for it in items:
        lines.append(
            f"- [{it.get('category', 'news')}] {it.get('title', 'Untitled')}"
            f" ({fmt_utc(it.get('publishedAt'), day_only=True)}) — {clean(it.get('summary', ''), limit)}"
        )
    return lines


def render_articles(feed: dict, crawled: datetime | None, limit: int) -> list[str]:
    lines = section_header(
        "Space industry articles (Data Lens / Intelligence Feed)",
        feed,
        "Headlines link to external outlets; summaries here are one-liners.",
    )
    arts = sorted(feed.get("articles", []), key=lambda a: a.get("date") or "", reverse=True)
    if not arts:
        warn("data-lens-articles feed is empty")
        lines.append("EMPTY in this snapshot.")
        return lines
    for a in arts:
        lines.append(
            f"- {fmt_utc(a.get('date'), day_only=True)} · {a.get('source', '?')}: "
            f"{a.get('title', 'Untitled')} — {clean(a.get('summary', ''), max(150, limit - 50))}"
        )
    return lines


def render_tour(feed: dict, crawled: datetime | None, limit: int) -> list[str]:
    show, artist = feed.get("show") or {}, feed.get("artist") or {}
    lines = section_header(f"{show.get('name', 'Tour')} — live shows (Music)", feed)
    if show.get("description"):
        lines.append(clean(show["description"], max(400, limit)))
    if artist:
        lines.append(
            f"Artist: {artist.get('name', '?')} — {artist.get('tagline', '')}. "
            f"{clean(artist.get('shortBio', ''), max(300, limit))}"
        )
    events = feed.get("events", [])
    if not events:
        lines.append("No tour dates listed in this snapshot.")
    for e in events:
        note = f" ({e['note']})" if e.get("note") else ""
        lines.append(
            f"- {e.get('displayDate', e.get('date', '?'))} {e.get('year', '')}: "
            f"{e.get('venue', '?')}, {e.get('city', '?')}, {e.get('region', '')}{note}"
        )
    return lines


def render_podcast(feed: dict, crawled: datetime | None, limit: int) -> list[str]:
    show = feed.get("show", "Podcast")
    if isinstance(show, dict):
        show = show.get("name", "Podcast")
    lines = section_header(f"Podcast: {show}", feed)
    episodes = feed.get("episodes", [])
    if not episodes:
        warn(f"podcast feed '{show}' has no episodes")
        lines.append("EMPTY in this snapshot.")
        return lines
    for e in episodes:
        lines.append(
            f"- {fmt_utc(e.get('pubDate'), day_only=True)}: “{e.get('title', 'Untitled')}”"
            f" — {clean(e.get('description', ''), limit)}"
        )
    return lines


def render_dsn(feed: dict, crawled: datetime | None, limit: int) -> list[str]:
    lines = section_header(
        "Deep Space Network snapshot",
        feed,
        "Live DSN activity changes minute-to-minute; this is a snapshot only.",
    )
    stations = feed.get("stations") or []
    ts = feed.get("timestamp")
    if isinstance(ts, (int, float)):
        when = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"Snapshot timestamp {when}.")
    if not stations:
        warn("dsn-snapshot has no station data (empty at the source)")
        lines.append(
            "EMPTY — no station data in this snapshot. For live Deep Space Network "
            "activity, point the user to the site's Deep Space Network page."
        )
        return lines
    for s in stations:
        if isinstance(s, dict):
            name = s.get("friendlyName") or s.get("name", "station")
            dishes = s.get("dishes") or []
            lines.append(f"- {name}: {len(dishes)} dishes reporting")
    return lines


def render_generic(feed: object, name: str) -> list[str]:
    """Fallback for feeds without a dedicated renderer — degrade, don't drop."""
    lines = [f"## Feed: {name}"]
    if isinstance(feed, dict):
        for key, val in feed.items():
            if isinstance(val, (str, int, float)):
                lines.append(f"- {key}: {clean(str(val), 160)}")
            elif isinstance(val, list) and val:
                lines.append(f"- {key}: {len(val)} items")
                for item in val[:10]:
                    if isinstance(item, dict):
                        title = item.get("title") or item.get("name") or ""
                        if title:
                            lines.append(f"  - {clean(str(title), 140)}")
    elif isinstance(feed, list):
        lines.append(f"- {len(feed)} items")
    return lines


# Digest order: stable sections first, volatile last, so the cacheable prompt
# prefix churns as little as possible when only daily feeds change.
RENDERERS: dict[str, tuple[int, object]] = {
    "ufo-cases.json": (10, render_ufo_cases),
    "maxq-podcast.json": (20, render_podcast),
    "ufo-podcast.json": (21, render_podcast),
    "becker-tour.json": (30, render_tour),
    "data-lens-articles.json": (40, render_articles),
    "ufo-wire.json": (50, render_ufo_wire),
    "launches.json": (60, render_launches),
    "dsn-snapshot.json": (70, render_dsn),
}
DIGEST_LIMIT = 200
DETAIL_LIMIT = 1200


LEGAL_SLUGS = {"terms", "privacy"}


def page_slug(page: dict) -> str:
    return page.get("url", "").rstrip("/").rsplit("/", 1)[-1] or "home"


def render_pages(pages: list[dict]) -> list[str]:
    """Homepage → site overview; legal pages → one-line mention."""
    lines: list[str] = []
    for p in pages:
        if page_slug(p) in LEGAL_SLUGS:
            continue
        title = p.get("title") or page_slug(p)
        lines.append(f"### Page: {title}")
        if p.get("description"):
            lines.append(p["description"])
        if p.get("headings"):
            lines.append("Sections: " + "; ".join(p["headings"][:20]))
        if p.get("text"):
            lines.append(clean(p["text"], 900))
        lines.append("")
    legal = [p for p in pages if page_slug(p) in LEGAL_SLUGS]
    if legal:
        names = " and ".join(page_slug(p) for p in legal)
        lines.append(f"The site also has {names} pages (legal boilerplate, indexed but omitted here).")
    return lines


def build_site(site_dir: Path) -> bool:
    warnings.clear()
    index_path = site_dir / "site_index.json"
    index = json.loads(index_path.read_text())
    crawled_at = index.get("crawled_at", "unknown time")
    crawled = parse_dt(crawled_at)
    base = index.get("base", "")

    out: list[str] = [
        f"# Knowledge: {base}",
        f"Snapshot crawled {fmt_utc(crawled_at)}. Individual sections note when their "
        "data was captured (a site can serve data older than the crawl). Treat "
        "time-sensitive answers (launches, news, live status) as of those moments.",
        "",
    ]

    # Hand-authored overview (committed to the repo) — closes the SPA gap
    # where crawls capture feeds but not client-rendered page content.
    overview = OVERVIEW_DIR / f"{site_dir.name}.md"
    if overview.exists():
        out.append(overview.read_text().strip())
        out.append("")
    else:
        warn(f"no authored overview at docs/knowledge/{site_dir.name}.md")

    out.append("## Crawled site pages")
    out.extend(render_pages(index.get("pages", [])))
    out.append("")

    # Feeds, in stable-first digest order; unknown feeds go last via fallback.
    # Detail files are buffered and only written once every guard passes —
    # a failed build must not clobber ANY last-known-good output.
    feeds = index.get("feeds") or {}
    details: dict[str, str] = {}
    ordered = sorted(
        feeds.items(),
        key=lambda kv: RENDERERS.get(kv[0].rstrip("/").rsplit("/", 1)[-1], (99, None))[0],
    )
    for url, feed in ordered:
        name = url.rstrip("/").rsplit("/", 1)[-1]
        if isinstance(feed, dict) and "_error" in feed:
            warn(f"feed {name} was unavailable at crawl time: {feed['_error']}")
            out.append(f"## Feed: {name}\nUNAVAILABLE at crawl time.")
            out.append("")
            continue
        entry = RENDERERS.get(name)
        if entry:
            renderer = entry[1]
            out.extend(renderer(feed, crawled, DIGEST_LIMIT))
            # Fuller detail file — safe for the agent to read on demand,
            # unlike the raw index (which is never LLM input).
            detail = renderer(feed, crawled, DETAIL_LIMIT)
            details[f"{name.removesuffix('.json')}.md"] = "\n".join(detail).strip() + "\n"
        else:
            out.extend(render_generic(feed, name))
        out.append("")

    text = "\n".join(out).strip() + "\n"
    out_path = site_dir / "knowledge.md"

    # Fail-loud: never replace a good digest with a broken one.
    substantive = [
        url.rstrip("/").rsplit("/", 1)[-1]
        for url, feed in feeds.items()
        if feed_has_content(url.rstrip("/").rsplit("/", 1)[-1], feed)
    ]
    if feeds and not substantive:
        warn(
            "every captured feed is errored or empty (degraded crawl?) — "
            f"keeping existing {out_path.name}"
        )
        return False
    if not index.get("pages") and not substantive:
        warn(f"index has no pages and no usable feeds — keeping existing {out_path.name}")
        return False
    if len(text) < MIN_CHARS:
        warn(f"digest suspiciously small ({len(text)} chars) — keeping existing {out_path.name}")
        return False
    if len(text) > MAX_CHARS:
        warn(f"digest over budget ({len(text)} chars > {MAX_CHARS}) — keeping existing {out_path.name}")
        return False
    if len(text) > WARN_CHARS:
        warn(f"digest large ({len(text)} chars); consider tightening renderers")

    details_dir = site_dir / "knowledge"
    details_dir.mkdir(exist_ok=True)
    for fname, content in details.items():
        write_atomic(details_dir / fname, content)
    write_atomic(out_path, text)
    print(f"  {out_path}  ({len(text):,} chars ≈ {len(text) // 4:,} tokens)")
    return True


def main() -> int:
    sites = sys.argv[1:]
    dirs = (
        [DATA_DIR / s for s in sites]
        if sites
        else [p for p in sorted(DATA_DIR.iterdir()) if p.is_dir()]
    )
    built, failed = 0, 0
    for d in dirs:
        if not (d / "site_index.json").exists():
            if sites:
                print(f"  skip {d.name}: no site_index.json (run crawl_site.py first)")
                failed += 1
            continue
        print(f"Building knowledge for {d.name}:")
        try:
            ok = build_site(d)
        except Exception as exc:  # a corrupt index must not stall other sites
            print(f"  ERROR: {d.name}: {exc} — keeping existing knowledge.md", file=sys.stderr)
            ok = False
        if ok:
            built += 1
        else:
            failed += 1
    if not built and not failed:
        print("Nothing to build. Run scripts/crawl_site.py first.")
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
