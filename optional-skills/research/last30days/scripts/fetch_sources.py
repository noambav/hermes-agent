#!/usr/bin/env python3
"""Keyless multi-source fetcher for the last30days skill.

Queries three free JSON APIs over a recency window and emits scored,
normalized items as JSON or markdown:

- Hacker News  : hn.algolia.com/api/v1/search (created_at_i numeric filters)
- Reddit       : www.reddit.com/search.json (t=month; may 403 — see SKILL.md)
- Polymarket   : gamma-api.polymarket.com/public-search (active events)

Pure stdlib. No API keys. Endpoints and query parameters ported from
mvanhorn/last30days-skill (MIT). X/YouTube/web coverage is handled by the
hosting agent's own search tools, not this script.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "last30days-hermes/0.1 (research skill; +https://github.com/mvanhorn/last30days-skill)"

HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search"
REDDIT_SEARCH_URL = "https://www.reddit.com/search.json"
POLYMARKET_SEARCH_URL = "https://gamma-api.polymarket.com/public-search"

MIN_HN_POINTS = 2  # upstream filters low-engagement stories client-side


def _get_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def engagement_score(score: int, num_comments: int) -> float:
    """Engagement-based relevance in [0, 1] (ported from upstream reddit_public)."""
    score_component = min(1.0, max(0.0, score / 500.0))
    comments_component = min(1.0, max(0.0, num_comments / 200.0))
    return round((score_component * 0.6) + (comments_component * 0.4), 3)


def date_to_unix(date_str: str) -> int:
    """YYYY-MM-DD -> Unix timestamp at start of day UTC."""
    y, m, d = (int(p) for p in date_str.split("-"))
    return int(_dt.datetime(y, m, d, tzinfo=_dt.timezone.utc).timestamp())


def window(days: int) -> tuple[str, str]:
    today = _dt.datetime.now(_dt.timezone.utc).date()
    return ((today - _dt.timedelta(days=days)).isoformat(), today.isoformat())


def search_hackernews(topic: str, from_date: str, to_date: str, limit: int = 30) -> list[dict]:
    from_ts = date_to_unix(from_date)
    to_ts = date_to_unix(to_date) + 86400  # include end date
    params = {
        "query": topic,
        "tags": "story",
        "numericFilters": f"created_at_i>{from_ts},created_at_i<{to_ts}",
        "hitsPerPage": str(limit * 2),  # overfetch, then filter low engagement
    }
    # Algolia ANDs query tokens; mark all-but-first optional so multi-word
    # topics rank by token overlap instead of requiring every word.
    tokens = topic.split()
    if len(tokens) > 1:
        params["optionalWords"] = " ".join(tokens[1:])
    data = _get_json(f"{HN_SEARCH_URL}?{urllib.parse.urlencode(params)}")
    items = []
    for hit in data.get("hits", []):
        points = hit.get("points") or 0
        if points <= MIN_HN_POINTS:
            continue
        object_id = hit.get("objectID", "")
        items.append({
            "source": "hackernews",
            "title": hit.get("title") or "",
            "url": hit.get("url") or f"https://news.ycombinator.com/item?id={object_id}",
            "discussion_url": f"https://news.ycombinator.com/item?id={object_id}",
            "date": (hit.get("created_at") or "")[:10],
            "points": points,
            "comments": hit.get("num_comments") or 0,
            "relevance": engagement_score(points, hit.get("num_comments") or 0),
        })
    items.sort(key=lambda i: i["relevance"], reverse=True)
    return items[:limit]


def search_reddit(topic: str, limit: int = 25, subreddit: str | None = None) -> list[dict]:
    """Reddit public JSON search, t=month. May return HTTP 403 (endpoint is
    unreliable per upstream); callers should fall back to agent web search."""
    if subreddit:
        base = f"https://www.reddit.com/r/{subreddit.removeprefix('r/')}/search.json"
        params = {"q": topic, "restrict_sr": "on", "sort": "relevance", "t": "month",
                  "limit": str(limit), "raw_json": "1"}
    else:
        base = REDDIT_SEARCH_URL
        params = {"q": topic, "sort": "relevance", "t": "month",
                  "limit": str(limit), "raw_json": "1"}
    data = _get_json(f"{base}?{urllib.parse.urlencode(params)}")
    items, seen = [], set()
    for child in (data.get("data") or {}).get("children", []):
        post = child.get("data") or {}
        permalink = post.get("permalink") or ""
        url = f"https://www.reddit.com{permalink}"
        if url in seen:
            continue
        seen.add(url)
        score = post.get("score") or 0
        num_comments = post.get("num_comments") or 0
        created = post.get("created_utc")
        items.append({
            "source": "reddit",
            "title": post.get("title") or "",
            "url": url,
            "subreddit": post.get("subreddit") or "",
            "date": _dt.datetime.fromtimestamp(created, tz=_dt.timezone.utc).date().isoformat()
                    if created else "",
            "points": score,
            "comments": num_comments,
            "relevance": engagement_score(score, num_comments),
        })
    items.sort(key=lambda i: i["relevance"], reverse=True)
    return items[:limit]


def search_polymarket(topic: str, pages: int = 3, limit: int = 15) -> list[dict]:
    """Active prediction markets matching the topic, sorted by volume."""
    events: dict[str, dict] = {}
    for page in range(1, pages + 1):
        params = {"q": topic, "page": str(page),
                  "events_status": "active", "keep_closed_markets": "0"}
        try:
            data = _get_json(f"{POLYMARKET_SEARCH_URL}?{urllib.parse.urlencode(params)}")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            break
        batch = data.get("events") or []
        if not batch:
            break
        for ev in batch:
            eid = str(ev.get("id") or ev.get("slug") or "")
            if eid and eid not in events:
                events[eid] = ev
    items = []
    for eid, ev in events.items():
        slug = ev.get("slug") or ""
        volume = float(ev.get("volume1mo") or ev.get("volume") or 0)
        items.append({
            "source": "polymarket",
            "title": ev.get("title") or "",
            "url": f"https://polymarket.com/event/{slug}" if slug
                   else f"https://polymarket.com/event/{eid}",
            "volume_1mo": volume,
            "liquidity": float(ev.get("liquidity") or 0),
        })
    items.sort(key=lambda i: i["volume_1mo"], reverse=True)
    return items[:limit]


def render_markdown(topic: str, from_date: str, to_date: str, results: dict) -> str:
    lines = [f"# last30days: {topic} ({from_date} to {to_date})", ""]
    for source, payload in results.items():
        items = payload["items"]
        status = payload["status"]
        lines.append(f"## {source} ({len(items)} items, status: {status})")
        for it in items:
            meta = []
            if "points" in it:
                meta.append(f"{it['points']} pts, {it['comments']} comments")
            if "volume_1mo" in it:
                meta.append(f"${it['volume_1mo']:,.0f} 30-day volume")
            if it.get("date"):
                meta.append(it["date"])
            lines.append(f"- [{it['title']}]({it['url']}) ({'; '.join(meta)})")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Keyless last-30-days source fetcher")
    ap.add_argument("topic")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--sources", default="hackernews,reddit,polymarket",
                    help="comma list: hackernews,reddit,polymarket")
    ap.add_argument("--limit", type=int, default=20, help="max items per source")
    ap.add_argument("--subreddit", default=None, help="scope reddit search to one subreddit")
    ap.add_argument("--format", choices=["json", "md"], default="md")
    args = ap.parse_args(argv)

    from_date, to_date = window(args.days)
    wanted = {s.strip() for s in args.sources.split(",") if s.strip()}
    fetchers = {
        "hackernews": lambda: search_hackernews(args.topic, from_date, to_date, args.limit),
        "reddit": lambda: search_reddit(args.topic, args.limit, args.subreddit),
        "polymarket": lambda: search_polymarket(args.topic, limit=args.limit),
    }
    results: dict[str, dict] = {}
    for name in ("hackernews", "reddit", "polymarket"):
        if name not in wanted:
            continue
        try:
            results[name] = {"status": "ok", "items": fetchers[name]()}
        except Exception as exc:  # degraded, not fatal — mirror upstream behavior
            results[name] = {"status": f"error: {exc}", "items": []}
            print(f"[last30days] {name} failed: {exc}", file=sys.stderr)

    if args.format == "json":
        print(json.dumps({"topic": args.topic, "from": from_date, "to": to_date,
                          "results": results}, indent=2))
    else:
        print(render_markdown(args.topic, from_date, to_date, results))
    # exit 0 even on partial coverage (upstream default); failures are annotated
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
