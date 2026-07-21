---
title: "Last30Days — Research what people said about a topic in the last 30 days"
sidebar_label: "Last30Days"
description: "Research what people said about a topic in the last 30 days"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Last30Days

Research what people said about a topic in the last 30 days.

## Skill metadata

| | |
|---|---|
| Source | Optional — install with `hermes skills install official/research/last30days` |
| Path | `optional-skills/research/last30days` |
| Version | `0.1.0` |
| Author | Matt Van Horn (mvanhorn), Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `Research`, `Social-Media`, `Trends`, `News` |
| Related skills | [`polymarket`](/docs/user-guide/skills/bundled/research/research-polymarket), [`duckduckgo-search`](/docs/user-guide/skills/optional/research/research-duckduckgo-search) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# last30days

Researches a topic across Hacker News, Reddit, Polymarket, X, YouTube, and the
general web over a recent time window (default 30 days), then synthesizes a
grounded summary of what people are actually saying, with engagement signals
and citations. The bundled fetch script is pure Python stdlib and fully
keyless; API keys are optional enhancers only. This is a slimmed Hermes port of
the upstream last30days skill (github.com/mvanhorn/last30days-skill, MIT) — it
does not include upstream's TikTok/Instagram/Bluesky/Digg integrations or its
setup wizard.

## When to Use

- "What are people saying about X lately?" / community-sentiment questions
- Recency-bound research: launches, earnings reactions, drama, hiring signals
- Comparing tools/products by recent community chatter ("X vs Y")
- Checking prediction-market odds alongside social discussion
- NOT for deep historical research or single-document lookups — use plain
  `web_search`/`web_extract` for those

## Prerequisites

- Python 3.10+ (stdlib only — no pip installs)
- Network access to reddit.com, hn.algolia.com, gamma-api.polymarket.com
- No env vars required. Everything runs keyless in degraded-but-useful mode.
  Optional enhancers (document in the user's own `.env`, never required):
  - `XAI_API_KEY` — richer X/Twitter coverage upstream; in this port, X
    coverage comes from `web_search` instead
  - `BRAVE_API_KEY` — upstream web-search backend; unnecessary here because
    Hermes has native `web_search`

## How to Run

Run the fetch script through the `terminal` tool, then synthesize:

```
python3 ~/.hermes/skills/research/last30days/scripts/fetch_sources.py "your topic" --days 30 --format md
```

Cover X, YouTube, and the general web with Hermes `web_search` (the script
deliberately does not scrape those), then write the summary per the Procedure.

## Quick Reference

| Command | Purpose |
|---|---|
| `fetch_sources.py "topic"` | All keyless sources, markdown output |
| `fetch_sources.py "topic" --format json` | Machine-readable output |
| `fetch_sources.py "topic" --days 7` | Narrower recency window |
| `fetch_sources.py "topic" --sources hackernews,polymarket` | Subset of sources |
| `fetch_sources.py "topic" --subreddit LocalLLaMA` | Scope Reddit to one sub |
| `fetch_sources.py "topic" --limit 10` | Cap items per source |

Sources: `hackernews` (Algolia, date-filtered), `reddit` (public JSON search,
`t=month`), `polymarket` (active events by 30-day volume).

## Procedure

1. Run the fetcher via `terminal`:
   `python3 ~/.hermes/skills/research/last30days/scripts/fetch_sources.py "TOPIC" --format md`
   Note each source's `status:` line — `error` means degraded coverage for
   that source, not a failed run.
2. Fill the gaps with Hermes-native tools (batch these searches):
   - `web_search`: `TOPIC site:x.com` or `TOPIC twitter reaction` for X chatter
   - `web_search`: `TOPIC site:youtube.com` (or `TOPIC review video`) for YouTube
   - `web_search`: `TOPIC` plus a recency phrase like "this month" for news/web
3. Pull full text of the 2-4 most-cited pages with `web_extract` when a
   headline alone can't support a claim.
4. Discard anything clearly older than the window. The script date-filters HN
   and Reddit; web results need manual checking against publish dates.
5. Synthesize. Upstream's output contract, kept here because it works:
   - Lead with `What I learned:` then bold-lead-in paragraphs — no invented
     title, no `##` section headers in the body
   - Quote real people verbatim with source attribution (subreddit, HN
     thread, channel); prefer high-engagement items (the script pre-sorts
     by an engagement score: 0.6·score + 0.4·comments, each capped)
   - End with a numbered `KEY PATTERNS from the research:` list
   - Never fabricate quotes, titles, or engagement numbers; if a source
     returned nothing or errored, say "partial coverage" — not "nothing
     was said on &lt;source>"
   - Include Polymarket odds only when a market genuinely matches the topic
6. For comparison topics ("A vs B"), run steps 1-3 once per entity, then
   structure as: verdict, per-entity findings, head-to-head, bottom line.

## Pitfalls

- **Reddit public JSON returns HTTP 403 intermittently** — upstream migrated
  off it for this reason. When the `reddit` source reports an error, fall back
  to `web_search` with `TOPIC site:reddit.com` and treat Reddit as partial
  coverage. Do not retry in a loop.
- HN Algolia rejects `points>N` in `numericFilters` (HTTP 400) — the script
  filters low-engagement stories client-side instead; don't "fix" that.
- Polymarket's search matches loosely; a topic like "React hooks" can return
  unrelated political markets. Drop non-matching events during synthesis.
- Engagement scores compare items *within* a source only — a 500-point HN
  story and a 500-upvote Reddit post are not equivalent audiences.
- Windows: invoke as `python` if `python3` is not on PATH; the script itself
  is portable (no `/tmp`, no shell pipelines).
- Don't dump the script's raw item list as the answer — it is evidence for
  synthesis, not output.

## Verification

```
python3 ~/.hermes/skills/research/last30days/scripts/fetch_sources.py "artificial intelligence" --sources hackernews --limit 3 --format json
```

Should print JSON with `"status": "ok"` and up to 3 dated HN stories from the
last 30 days. Exit code 0 even on partial source failures.
