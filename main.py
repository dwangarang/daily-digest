"""
Daily Digest — main orchestrator.

Run modes:
  python main.py digest       → Full pipeline: ingest, process, curate, send
  python main.py ingest       → Only fetch new content from sources
  python main.py process      → Process up to 25 unprocessed articles via Batch API
  python main.py sweep        → Process ALL unprocessed articles (for initial setup)
  python main.py replies      → Only check inbox for replies/feedback
  python main.py test         → Dry-run: ingest + process + curate, preview only
  python main.py add-concept  → Interactively add a concept to the digest pool
"""

import os
import sys
import json
import yaml
import hashlib
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from data.db import (
    init_db, article_exists, save_article, update_article_processing,
    get_unprocessed_articles, save_digest,
    get_pending_manual_items, mark_manual_processed,
    get_pending_concepts, mark_concept_processed,
)
from sources.rss import fetch_rss_source
from sources.api import fetch_api_source
from sources.scraper import fetch_scrape_source, fetch_evergreen_source, fetch_full_article
from processing.summarizer import process_articles_batch, process_article, process_concept
from processing.curator import curate_digest
from processing.repetition import (
    create_repetitions_for_digest, get_callback_questions, mark_callbacks_shown
)
from delivery.sender import render_email, send_email
from delivery.reply_parser import check_replies


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    if not config_path.exists():
        print("[!] config.yaml not found. Copy config.example.yaml to config.yaml first.")
        sys.exit(1)
    with open(config_path) as f:
        return yaml.safe_load(f)


# ---- Keyword pre-filter for high-volume sources ----
# Any match on title + first 500 chars of content → article is saved and processed.
# Sources opt in via  filter: keywords  in config.yaml.

TOPIC_FILTER_KEYWORDS = {
    "AI & Machine Learning": [
        "ai", "llm", "gpt", "language model", "machine learning", "deep learning",
        "neural", "openai", "anthropic", "gemini", "claude", "agent", "foundation model",
        "transformer", "inference", "benchmark", "generative", "chatgpt", "deepseek",
        "multimodal", "reasoning model",
    ],
    "GTM & Product Strategy": [
        "saas", "gtm", "go-to-market", "product", "growth", "startup", "b2b",
        "revenue", "sales", "marketing", "pricing", "customer", "churn", "retention",
        "enterprise", "product-market fit", "category creation",
    ],
    "Investing & Mental Models": [
        "invest", "valuation", "venture", "market", "portfolio", "risk", "capital",
        "return", "fund", "equity", "mental model", "framework", "decision",
        "economics", "economic", "interest rate", "inflation",
    ],
    "China-Tech & Geopolitics": [
        "china", "chinese", "beijing", "xi", "ccp", "taiwan", "asean", "semiconductor",
        "geopolit", "byd", "alibaba", "tencent", "huawei", "deepseek", "baidu",
        "supply chain", "tariff", "export control",
    ],
    "General Interest": [],  # empty = no filtering for this topic
}


def _passes_keyword_filter(article: dict, source_topics: list) -> bool:
    """Return True if the article's title/excerpt matches any keyword for its topics."""
    text = (article.get("title", "") + " " + article.get("raw_content", "")[:500]).lower()
    for topic in source_topics:
        keywords = TOPIC_FILTER_KEYWORDS.get(topic, [])
        if not keywords:          # General Interest or unknown topic = always pass
            return True
        if any(kw in text for kw in keywords):
            return True
    return False


# ---- Shared helper ----

def _save_processed_article(article_id: str, result: dict):
    """Persist all Claude-generated fields for an article."""
    insight = result.get("insight", "")
    update_article_processing(
        article_id=article_id,
        summary=insight,
        takeaways=result.get("key_takeaways", []),
        tags=result.get("tags", []),
        relevance_score=result.get("relevance_score", 0.0),
        think_about_this=result.get("think_about_this", ""),
        core_concept=result.get("core_concept", ""),
        related_search_terms=[],
        insight=insight,
        so_what=result.get("so_what", ""),
        contrarian_angle=result.get("contrarian_angle", ""),
        further_reading=result.get("further_reading", []),
        think_framework=result.get("think_framework", ""),
    )


# ---- Pipeline stages ----

def stage_ingest(config: dict) -> int:
    """Fetch new content from all configured sources. Returns count of new articles."""
    sources = config.get("sources", [])
    new_count = 0

    for source in sources:
        source_type = source.get("type", "rss")
        print(f"  Fetching: {source['name']} ({source_type})")

        if source_type == "rss":
            articles = fetch_rss_source(source)
        elif source_type == "api":
            articles = fetch_api_source(source)
        elif source_type == "scrape":
            articles = fetch_scrape_source(source)
        elif source_type == "evergreen":
            articles = fetch_evergreen_source(source)
        else:
            print(f"    [!] Unknown source type: {source_type}")
            continue

        saved = 0
        filtered = 0
        for article in articles:
            if not article_exists(article["id"]):
                if source.get("filter") == "keywords":
                    if not _passes_keyword_filter(article, source.get("topics", [])):
                        filtered += 1
                        continue
                save_article(article)
                new_count += 1
                saved += 1

        status = f"{saved} saved"
        if filtered:
            status += f", {filtered} filtered"
        print(f"    {len(articles)} fetched → {status}")

    # Manual URL queue (from email replies)
    manual_items = get_pending_manual_items()
    if manual_items:
        print(f"  Processing {len(manual_items)} queued URL(s)...")
    for item in manual_items:
        url = item["url"]
        content = fetch_full_article(url)
        if content:
            article_id = hashlib.sha256(f"manual:{url}".encode()).hexdigest()[:16]
            article = {
                "id": article_id,
                "source_name": "Manual Queue",
                "title": item.get("note") or url,
                "url": url,
                "raw_content": content,
            }
            if not article_exists(article_id):
                save_article(article)
                new_count += 1
        mark_manual_processed(item["id"])

    # Concept queue (from email replies)
    concept_items = get_pending_concepts()
    if concept_items:
        print(f"  Processing {len(concept_items)} queued concept(s)...")
    for item in concept_items:
        print(f"    Concept: {item['name']}")
        result = process_concept(item["name"], item["explanation"], item["topic"], config)
        if result:
            article_id = hashlib.sha256(
                f"concept:{item['name']}:{item['added_at']}".encode()
            ).hexdigest()[:16]
            source_label = f"[Manual] {item['source']}" if item.get("source") else "[Manual Concept]"
            article = {
                "id": article_id,
                "source_name": source_label,
                "title": item["name"],
                "url": "",
                "raw_content": item["explanation"],
            }
            if not article_exists(article_id):
                save_article(article)
                _save_processed_article(article_id, result)
                create_repetitions_for_digest([{
                    "id": article_id, "title": item["name"], "url": "",
                    "summary": result.get("insight", ""),
                    "key_takeaways": result.get("key_takeaways", []),
                    "think_about_this": result.get("think_about_this", ""),
                    "core_concept": result.get("core_concept", item["name"]),
                }], config)
                new_count += 1
        mark_concept_processed(item["id"])

    return new_count


def stage_process(config: dict, limit: int = 25) -> int:
    """
    Process unprocessed articles via Batch API. Returns count processed.
    Uses limit=25 for daily runs; sweep mode passes a higher limit.
    """
    articles = get_unprocessed_articles(limit=limit)
    processed = 0

    if not articles:
        print("  No unprocessed articles.")
        return 0

    print(f"  Sending {len(articles)} articles to Batch API...")
    results = process_articles_batch(articles, config)

    for article in articles:
        result = results.get(article["id"])
        if result:
            _save_processed_article(article["id"], result)
            processed += 1
        else:
            update_article_processing(
                article_id=article["id"],
                summary="[Processing failed]",
                takeaways=[], tags=[], relevance_score=0.0,
            )

    return processed


def stage_sweep(config: dict):
    """Process ALL unprocessed articles in batches. Used for initial evergreen setup."""
    total = 0
    batch_num = 0
    while True:
        remaining = get_unprocessed_articles(limit=1)
        if not remaining:
            break
        batch_num += 1
        print(f"\n  [Sweep batch {batch_num}]")
        count = stage_process(config, limit=50)
        total += count
        if count == 0:
            break
    print(f"\n  Sweep complete. {total} articles processed across {batch_num} batch(es).")


def stage_curate_and_send(config: dict, dry_run: bool = False) -> bool:
    """Curate a digest and send it. Returns True if sent successfully."""

    print("  Checking for email replies...")
    actions = check_replies(config)
    if actions:
        print(f"  Processed {len(actions)} reply actions")

    print("  Curating today's digest...")
    digest = curate_digest(config)
    if not digest:
        print("  [!] Not enough content. Run ingest + process first.")
        return False

    print(f"  Theme: {digest['theme']}")
    print(f"  Articles: {[a['title'][:40] for a in digest['articles']]}")

    callbacks = get_callback_questions(config)
    print(f"  Callbacks: {len(callbacks)} due for review")

    for article in digest["articles"]:
        for field in ("key_takeaways", "tags", "further_reading"):
            if isinstance(article.get(field), str):
                try:
                    article[field] = json.loads(article[field])
                except (json.JSONDecodeError, TypeError):
                    article[field] = []
        if not article.get("think_about_this"):
            article["think_about_this"] = ""

    print("  Rendering email...")
    html = render_email(digest, callbacks, config)

    if dry_run:
        preview_path = Path(__file__).parent / "data" / "preview.html"
        with open(preview_path, "w") as f:
            f.write(html)
        print(f"  [DRY RUN] Preview saved to {preview_path}")
        print(f"  Open in browser: file://{preview_path.resolve()}")
        return True

    print("  Sending digest...")
    success = send_email(html, digest)

    if success:
        article_ids = [a["id"] for a in digest["articles"]]
        save_digest(digest["theme"], digest.get("theme_description", ""), article_ids)
        create_repetitions_for_digest(digest["articles"], config)

        callback_ids = [cb["repetition_id"] for cb in callbacks]
        if callback_ids:
            mark_callbacks_shown(callback_ids, config)

        print("  [✓] Digest pipeline complete!")

    return success


def stage_add_concept(config: dict):
    """Interactive mode: add a concept directly to the digest pool and repetition queue."""
    print("\nAdd a concept to your digest and spaced repetition system.\n")

    concept_name = input("Concept name: ").strip()
    if not concept_name:
        print("  [!] Concept name is required.")
        return

    explanation = input("Your explanation (in your own words): ").strip()
    if not explanation:
        print("  [!] Explanation is required.")
        return

    topics = config.get("topics", [])
    print("\nTopics:")
    for i, t in enumerate(topics, 1):
        print(f"  {i}. {t['name']}")
    print(f"  {len(topics) + 1}. Other")

    topic_choice = input(f"Select topic (1-{len(topics) + 1}): ").strip()
    try:
        idx = int(topic_choice) - 1
        topic = topics[idx]["name"] if 0 <= idx < len(topics) else "General Interest"
    except (ValueError, IndexError):
        topic = "General Interest"

    source = input("Source (optional, e.g. 'INSEAD Week 3', or Enter to skip): ").strip()

    print(f"\n  Processing '{concept_name}' through Claude...")
    result = process_concept(concept_name, explanation, topic, config)
    if not result:
        print("  [!] Failed to process concept.")
        return

    article_id = hashlib.sha256(
        f"concept:{concept_name}:{datetime.now().isoformat()}".encode()
    ).hexdigest()[:16]
    source_label = f"[Manual] {source}" if source else "[Manual Concept]"

    save_article({
        "id": article_id, "source_name": source_label,
        "title": concept_name, "url": "", "raw_content": explanation,
    })
    _save_processed_article(article_id, result)
    create_repetitions_for_digest([{
        "id": article_id, "title": concept_name, "url": "",
        "summary": result.get("insight", ""),
        "key_takeaways": result.get("key_takeaways", []),
        "think_about_this": result.get("think_about_this", ""),
        "core_concept": result.get("core_concept", concept_name),
    }], config)

    intervals = config.get("repetition", {}).get("intervals", [1])
    print(f"\n  [✓] '{concept_name}' added.")
    print(f"      Topic: {topic}")
    print(f"      First review in {intervals[0]} day(s)")
    if result.get("think_about_this"):
        print(f"      First question: {result['think_about_this']}")


# ---- Entry point ----

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "digest"

    print(f"\n{'='*50}")
    print(f"  Daily Digest — {mode.upper()} mode")
    print(f"{'='*50}\n")

    init_db()
    config = load_config()

    if mode == "ingest":
        count = stage_ingest(config)
        print(f"\n  Done. {count} new articles ingested.")

    elif mode == "process":
        count = stage_process(config)
        print(f"\n  Done. {count} articles processed.")

    elif mode == "sweep":
        print("Processing all unprocessed articles (initial setup sweep)...")
        stage_sweep(config)

    elif mode == "digest":
        print("[1/3] Ingesting new content...")
        stage_ingest(config)
        print("\n[2/3] Processing articles...")
        stage_process(config)
        print("\n[3/3] Curating and sending digest...")
        stage_curate_and_send(config)

    elif mode == "test":
        print("[1/3] Ingesting new content...")
        stage_ingest(config)
        print("\n[2/3] Processing articles...")
        stage_process(config)
        print("\n[3/3] Curating digest (DRY RUN)...")
        stage_curate_and_send(config, dry_run=True)

    elif mode == "replies":
        print("Checking for replies...")
        actions = check_replies(config)
        print(f"  {len(actions)} actions processed.")

    elif mode == "add-concept":
        stage_add_concept(config)

    else:
        print(f"Unknown mode: {mode}")
        print("Usage: python main.py [digest|ingest|process|sweep|test|replies|add-concept]")
        sys.exit(1)


if __name__ == "__main__":
    main()
