import json

import pytest

from scripts import build_knowledge as bk


CRAWLED = "2026-07-15T23:47:54+00:00"


def make_index(tmp_path, feeds=None, pages=None):
    site = tmp_path / "testsite"
    site.mkdir()
    index = {
        "base": "https://example.com/",
        "crawled_at": CRAWLED,
        "pages": pages
        or [
            {
                "url": "https://example.com/",
                "title": "Example",
                "description": "An example site",
                "headings": ["Widgets"],
                "text": "Example homepage text",
                "chars": 21,
            }
        ],
        "feeds": feeds or {},
        "report": {},
    }
    (site / "site_index.json").write_text(json.dumps(index))
    return site


LAUNCH_FEED = {
    "fetchedAt": "2026-07-14T20:15:00Z",
    "results": [
        {
            "name": "Flown Rocket | Done",
            "status": {"name": "Launch Successful", "abbrev": "Success"},
            "net": "2026-07-14T01:28:17Z",
            "net_precision": {"name": "Second"},
            "launch_service_provider": {"name": "SpaceCo"},
            "mission": {"description": "Already flew.", "orbit": {"name": "LEO"}},
            "pad": {"location": {"name": "Pad A"}},
        },
        {
            "name": "Upcoming Rocket | Soon",
            "status": {"name": "Go for Launch", "abbrev": "Go"},
            "net": "2026-07-16T20:32:00Z",
            "net_precision": {"name": "Second"},
            "launch_service_provider": {"name": "SpaceCo"},
            "mission": {"description": "Flies soon.", "orbit": {"name": "LEO"}},
            "pad": {"location": {"name": "Pad B"}},
        },
        {
            "name": "Vague Rocket | Someday",
            "status": {"name": "To Be Determined", "abbrev": "TBD"},
            "net": "2026-07-31T00:00:00Z",
            "net_precision": {"name": "Month"},
            "launch_service_provider": {"name": "OtherCo"},
            "mission": {},
            "pad": {},
        },
    ],
}


@pytest.fixture(autouse=True)
def small_min_chars(monkeypatch, tmp_path):
    # Tiny fixtures shouldn't trip the real suspiciously-small guard,
    # and no repo-authored overview should leak into tests.
    monkeypatch.setattr(bk, "MIN_CHARS", 100)
    monkeypatch.setattr(bk, "OVERVIEW_DIR", tmp_path / "no-overviews")


def build(site):
    assert bk.build_site(site) is True
    return (site / "knowledge.md").read_text()


def test_flown_launches_separated_from_upcoming(tmp_path):
    site = make_index(tmp_path, feeds={"https://example.com/data/launches.json": LAUNCH_FEED})
    text = build(site)
    recent = text.index("### Recently flown")
    upcoming = text.index("### Upcoming")
    assert recent < text.index("Flown Rocket", recent) < upcoming
    assert text.index("Upcoming Rocket", upcoming) > upcoming
    # The flown launch must not be listed under Upcoming
    assert "Flown Rocket" not in text[upcoming:]


def test_next_launch_rollup_skips_vague_precision(tmp_path):
    site = make_index(tmp_path, feeds={"https://example.com/data/launches.json": LAUNCH_FEED})
    text = build(site)
    line = next(l for l in text.splitlines() if l.startswith("Next scheduled launch:"))
    assert "Upcoming Rocket" in line
    assert "Vague Rocket" not in line
    # Month-precision NET must not be rendered as an exact timestamp
    assert "NET July 2026 (month precision, date not set)" in text


def test_feed_inner_timestamp_preferred_over_crawl_time(tmp_path):
    site = make_index(tmp_path, feeds={"https://example.com/data/launches.json": LAUNCH_FEED})
    text = build(site)
    assert "Data captured 2026-07-14 20:15 UTC" in text


def test_empty_dsn_marked_not_omitted(tmp_path):
    feed = {"stations": [], "timestamp": 1751566260000, "fetchedAt": "2026-07-03T18:11:00Z"}
    site = make_index(tmp_path, feeds={"https://example.com/data/dsn-snapshot.json": feed})
    text = build(site)
    assert "Deep Space Network" in text
    assert "EMPTY" in text


def test_fail_loud_keeps_last_known_good(tmp_path, monkeypatch):
    monkeypatch.setattr(bk, "MIN_CHARS", 2_000_000)  # force the small-digest guard
    site = make_index(tmp_path)
    good = site / "knowledge.md"
    good.write_text("LAST KNOWN GOOD")
    assert bk.build_site(site) is False
    assert good.read_text() == "LAST KNOWN GOOD"


def test_detail_files_written(tmp_path):
    site = make_index(tmp_path, feeds={"https://example.com/data/launches.json": LAUNCH_FEED})
    build(site)
    assert (site / "knowledge" / "launches.md").exists()


def test_authored_overview_included_when_present(tmp_path, monkeypatch):
    overview_dir = tmp_path / "overviews"
    overview_dir.mkdir()
    (overview_dir / "testsite.md").write_text("# Authored overview\nHand-written facts.")
    monkeypatch.setattr(bk, "OVERVIEW_DIR", overview_dir)
    site = make_index(tmp_path)
    text = build(site)
    assert "Hand-written facts." in text


def test_unknown_feed_uses_generic_fallback(tmp_path):
    feed = {"fetchedAt": "2026-07-10T00:00:00Z", "things": [{"title": "A thing"}]}
    site = make_index(tmp_path, feeds={"https://example.com/data/mystery.json": feed})
    text = build(site)
    assert "## Feed: mystery.json" in text
    assert "A thing" in text
