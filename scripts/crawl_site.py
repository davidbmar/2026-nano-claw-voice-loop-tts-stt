#!/usr/bin/env python3
"""Generic site crawler → knowledge index for nano-claw personas.

Crawls a site (same-host BFS), extracts readable text per page, and writes
data/<site>/site_index.json — a rebuildable knowledge artifact a voice persona
can answer questions from. Reusable for any site: Space Channel first.

Intended for sites whose owner has authorized indexing (robots.txt is not
consulted); the crawl is deliberately rate-limited so it never loads down the
target server.

Usage:
    python3 scripts/crawl_site.py https://www.spacechannel.com/ \
        [--max-pages 200] [--delay 0.7] [--name spacechannel] \
        [--feed https://www.spacechannel.com/data/ufo-wire.json ...]

Output: data/<name>/site_index.json
    { "base": ..., "crawled_at": ..., "pages": [{url, title, description,
      headings, text, chars}], "feeds": {url: parsed-json}, "report": {...} }

Only dependency beyond stdlib: httpx (present in .venv-test).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import deque
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

import httpx

UA = "nano-claw-site-crawler/1.0 (owner-authorized indexing)"
SKIP_EXTENSIONS = re.compile(
    r"\.(png|jpe?g|gif|webp|svg|ico|css|js|mjs|woff2?|ttf|eot|mp[34]|wav|ogg|webm"
    r"|pdf|zip|gz|tar|dmg|exe|json|xml|txt|map)$",
    re.IGNORECASE,
)
SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe"}
HEADING_TAGS = {"h1", "h2", "h3"}


class PageExtractor(HTMLParser):
    """Pull title, meta description, headings, visible text, and links."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self.headings: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._heading_buf: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        a = dict(attrs)
        if tag in SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in HEADING_TAGS:
            self._heading_buf = []
        elif tag == "meta" and a.get("name", "").lower() == "description":
            self.description = (a.get("content") or "").strip()
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in HEADING_TAGS and self._heading_buf is not None:
            heading = " ".join(self._heading_buf).strip()
            if heading:
                self.headings.append(heading)
            self._heading_buf = None

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title += text
        elif self._heading_buf is not None:
            self._heading_buf.append(text)
            self.text_parts.append(text)
        else:
            self.text_parts.append(text)


def normalize(base_url: str, href: str) -> str | None:
    """Resolve a link against the page and keep it if it's same-host HTML."""
    absolute, _frag = urldefrag(urljoin(base_url, href))
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https"):
        return None
    base_host = urlparse(base_url).netloc.lower().removeprefix("www.")
    host = parsed.netloc.lower().removeprefix("www.")
    if host != base_host:
        return None
    if SKIP_EXTENSIONS.search(parsed.path or ""):
        return None
    return absolute


def crawl(start: str, max_pages: int, delay: float) -> tuple[list[dict], dict]:
    seen: set[str] = set()
    queue: deque[str] = deque([start])
    pages: list[dict] = []
    errors: dict[str, str] = {}

    with httpx.Client(
        headers={"User-Agent": UA}, timeout=15.0, follow_redirects=True
    ) as client:
        while queue and len(pages) < max_pages:
            url = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            try:
                resp = client.get(url)
                ctype = resp.headers.get("content-type", "")
                if resp.status_code != 200 or "html" not in ctype:
                    errors[url] = f"{resp.status_code} {ctype.split(';')[0]}"
                    continue
                extractor = PageExtractor()
                extractor.feed(resp.text)
                text = re.sub(r"\s{2,}", " ", " ".join(extractor.text_parts)).strip()
                pages.append(
                    {
                        "url": str(resp.url),
                        "title": extractor.title.strip(),
                        "description": extractor.description,
                        "headings": extractor.headings[:20],
                        "text": text,
                        "chars": len(text),
                    }
                )
                print(f"  [{len(pages):3d}] {len(text):6d} chars  {url}")
                for href in extractor.links:
                    nxt = normalize(url, href)
                    if nxt and nxt not in seen:
                        queue.append(nxt)
            except Exception as exc:  # keep crawling past individual failures
                errors[url] = str(exc)
            time.sleep(delay)

    return pages, errors


def fetch_feeds(feed_urls: list[str]) -> dict:
    feeds: dict[str, object] = {}
    with httpx.Client(headers={"User-Agent": UA}, timeout=15.0) as client:
        for url in feed_urls:
            try:
                feeds[url] = client.get(url).json()
                print(f"  [feed] ok  {url}")
            except Exception as exc:
                feeds[url] = {"_error": str(exc)}
                print(f"  [feed] ERR {url}: {exc}")
    return feeds


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("start_url")
    ap.add_argument("--max-pages", type=int, default=200)
    ap.add_argument("--delay", type=float, default=0.7)
    ap.add_argument("--name", help="site slug (default: derived from host)")
    ap.add_argument("--feed", action="append", default=[],
                    help="JSON data feed to capture verbatim (repeatable)")
    args = ap.parse_args()

    slug = args.name or urlparse(args.start_url).netloc.lower().removeprefix(
        "www."
    ).split(".")[0]
    out_dir = Path(__file__).resolve().parents[1] / "data" / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Crawling {args.start_url} (max {args.max_pages} pages, "
          f"{args.delay}s delay) → data/{slug}/site_index.json")
    pages, errors = crawl(args.start_url, args.max_pages, args.delay)
    feeds = fetch_feeds(args.feed)

    index = {
        "base": args.start_url,
        "crawled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pages": pages,
        "feeds": feeds,
        "report": {
            "pages_indexed": len(pages),
            "errors": errors,
            "total_chars": sum(p["chars"] for p in pages),
        },
    }
    out_path = out_dir / "site_index.json"
    out_path.write_text(json.dumps(index, indent=1, ensure_ascii=False))

    print(f"\nDone: {len(pages)} pages, {index['report']['total_chars']:,} chars, "
          f"{len(errors)} errors → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
