"""
News Desk: at most N (default 2) current events per digest, analyzed in depth.

Event-centric, not article-centric, because the free tier of the news ecosystem
covers *events* redundantly even when individual outlets are paywalled:
1. Scan headlines across the configured free feeds (BBC, Guardian, etc.)
2. One cheap selection call (Haiku by default) clusters headlines into events
   and picks the few genuinely relevant to the reader's profile — often zero;
   the section only appears when something clears the bar.
3. Each selected event gets full-text fetches of its free coverage plus a
   web-search-enriched Sonnet analysis (what happened / why it matters /
   what to watch), then a grounded expert lens via the analyst module.

Events are persisted as articles (source "News Desk") so item-level feedback
commands (👍/explore) work on them, but they never re-enter the deep-read
candidate pool — news goes stale; get_unsent_articles excludes them.
"""

import os
import json
import time
import hashlib
from datetime import date

import feedparser
from anthropic import Anthropic

from data.db import save_article, update_article_processing, article_exists
from sources.scraper import fetch_full_article
from processing.analyst import generate_expert_analyses

MAX_HEADLINES = 80
MAX_ARTICLES_PER_EVENT = 2
MAX_AGE_SECONDS = 36 * 3600

client = None


def _get_client():
    global client
    if client is None:
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return client


def _parse_json(raw: str):
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
    if raw.endswith("```"):
        raw = raw.rsplit("```", 1)[0]
    return json.loads(raw.strip())


def _fetch_headlines(news_config: dict) -> list:
    """Collect recent headlines across all configured news feeds."""
    headlines = []
    now = time.time()
    for source in news_config.get("sources", []):
        try:
            feed = feedparser.parse(source["url"])
        except Exception as e:
            print(f"    [!] News feed failed ({source['name']}): {e}")
            continue
        for entry in feed.entries[:20]:
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published and (now - time.mktime(published)) > MAX_AGE_SECONDS:
                continue
            summary = entry.get("summary", "")
            # crude de-tag; feed summaries often carry HTML
            import re
            summary = re.sub(r"<[^>]+>", "", summary)[:200]
            headlines.append({
                "source": source["name"],
                "title": entry.get("title", ""),
                "summary": summary,
                "url": entry.get("link", ""),
            })
    return headlines[:MAX_HEADLINES]


def _select_events(headlines: list, config: dict) -> list:
    """One cheap call: cluster headlines into events, pick the relevant few."""
    news_config = config.get("news", {})
    max_events = news_config.get("max_events", 2)
    topic_names = [t["name"] for t in config.get("topics", [])]

    indexed = [
        {"index": i, "source": h["source"], "title": h["title"], "summary": h["summary"]}
        for i, h in enumerate(headlines)
    ]

    prompt = f"""You are scanning today's news for a specific reader. Cluster these headlines into underlying events (several headlines often cover one event), then select AT MOST {max_events} events that genuinely matter to this reader.

READER PROFILE:
{config.get('interest_profile', '')}

HEADLINES:
{json.dumps(indexed, indent=1)}

Selection bar: the event must be significant (not incremental churn) AND connect to the reader's actual interests. Selecting zero events is a valid answer on a slow news day — never pad.

Return ONLY JSON:
{{"events": [{{"title": "concise event name", "why": "one sentence on why this reader should care", "topic_tags": ["1-2 tags from {json.dumps(topic_names)}"], "headline_indices": [3, 17]}}]}}"""

    model = news_config.get("selection_model", "claude-haiku-4-5-20251001")
    response = _get_client().messages.create(
        model=model,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_json(response.content[0].text).get("events", [])[:max_events]


def _analyze_event(event: dict, headlines: list, config: dict) -> dict | None:
    """Full-text + web-search analysis of one selected event."""
    picked = [headlines[i] for i in event.get("headline_indices", [])
              if 0 <= i < len(headlines)]
    if not picked:
        return None

    coverage = []
    for h in picked[:MAX_ARTICLES_PER_EVENT]:
        text = fetch_full_article(h["url"])
        if text:
            coverage.append(f"--- {h['source']}: {h['title']} ---\n{text[:5000]}")
    if not coverage:
        # headline + summary only — still workable, web search fills gaps
        coverage = [f"--- {h['source']}: {h['title']} ---\n{h['summary']}" for h in picked]

    prompt = f"""Analyze this news event in depth for a specific reader. Use web search (max 2 searches) to verify facts and catch developments newer than the articles below.

EVENT: {event['title']}
WHY SELECTED: {event.get('why', '')}

COVERAGE:
{chr(10).join(coverage)}

READER PROFILE:
{config.get('interest_profile', '')}

After any searches, your FINAL message must be ONLY a JSON object:
{{
  "headline": "your own precise one-line framing of the event",
  "what_happened": "2-3 neutral factual sentences — what actually occurred, with specifics",
  "analysis": "3-4 sentences: why this matters, second-order implications. Find the event's natural implication first; connect to the reader's domains only where genuine. Do not open with the 'isn't X, it's Y' inversion template.",
  "watch_for": "one sentence: the concrete leading indicator that tells the reader which way this breaks"
}}"""

    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    response = _get_client().messages.create(
        model=model,
        max_tokens=1500,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}],
        messages=[{"role": "user", "content": prompt}],
    )

    text_blocks = [b.text for b in response.content if b.type == "text" and b.text.strip()]
    if not text_blocks:
        return None
    analysis = _parse_json(text_blocks[-1])
    analysis["links"] = [{"source": h["source"], "url": h["url"]} for h in picked]
    analysis["topic_tags"] = event.get("topic_tags", [])
    return analysis


def generate_news_desk(config: dict) -> list:
    """
    Produce today's News Desk events. Returns a list of dicts:
    {id, headline, what_happened, analysis, watch_for, lens, links}
    Empty list on a slow news day or any failure — the digest ships without it.
    """
    news_config = config.get("news", {})
    if not news_config.get("enabled", False):
        return []

    try:
        headlines = _fetch_headlines(news_config)
        if not headlines:
            print("    [news] No recent headlines fetched.")
            return []
        print(f"    [news] Scanning {len(headlines)} headlines...")

        events = _select_events(headlines, config)
        if not events:
            print("    [news] Nothing cleared the relevance bar today.")
            return []

        results = []
        for event in events:
            try:
                analysis = _analyze_event(event, headlines, config)
            except Exception as e:
                print(f"    [!] News analysis failed for '{event.get('title', '')[:40]}': {e}")
                continue
            if not analysis:
                continue

            article_id = hashlib.sha256(
                f"news:{date.today().isoformat()}:{analysis['headline']}".encode()
            ).hexdigest()[:16]

            if not article_exists(article_id):
                primary_url = analysis["links"][0]["url"] if analysis["links"] else ""
                save_article({
                    "id": article_id, "source_name": "News Desk",
                    "title": analysis["headline"], "url": primary_url,
                    "raw_content": analysis["what_happened"],
                })
                update_article_processing(
                    article_id=article_id,
                    summary=analysis["analysis"],
                    takeaways=[], tags=analysis.get("topic_tags", []),
                    relevance_score=0.5,
                    insight=analysis["analysis"],
                    context=analysis["what_happened"],
                    takeaway=analysis.get("watch_for", ""),
                )

            lens = generate_expert_analyses({
                "title": analysis["headline"],
                "source_name": "News Desk",
                "insight": analysis["analysis"],
                "tags": analysis.get("topic_tags", []),
            }, config)

            results.append({
                "id": article_id,
                "headline": analysis["headline"],
                "what_happened": analysis["what_happened"],
                "analysis": analysis["analysis"],
                "watch_for": analysis.get("watch_for", ""),
                "lens": lens,
                "links": analysis["links"],
            })
            print(f"    [news] Event: {analysis['headline'][:60]}")

        return results

    except Exception as e:
        print(f"  [!] News desk failed entirely ({e}) — digest ships without it.")
        return []
