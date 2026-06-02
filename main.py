"""
Daily Digest — main orchestrator.

Run modes:
  python main.py digest    → Run the full pipeline: ingest, process, curate, send
  python main.py ingest    → Only fetch new content from sources
  python main.py process   → Only process unprocessed articles through Claude
  python main.py replies   → Only check inbox for replies/feedback
  python main.py test      → Run a dry-run: ingest + process + curate, print but don't send

Each mode can be run independently, which is useful for debugging.
"""

import os
import sys
import json
import yaml
import hashlib
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv(Path(__file__).parent / ".env")

from data.db import (
    init_db, article_exists, save_article, update_article_processing,
    get_unprocessed_articles, save_digest,
    get_pending_manual_items, mark_manual_processed
)
from sources.rss import fetch_rss_source
from sources.api import fetch_api_source
from sources.scraper import fetch_scrape_source, fetch_evergreen_source, fetch_full_article
from processing.summarizer import process_article
from processing.curator import curate_digest
from processing.repetition import (
    create_repetitions_for_digest, get_callback_questions, mark_callbacks_shown
)
from delivery.sender import render_email, send_email
from delivery.reply_parser import check_replies


def load_config() -> dict:
    """Load config.yaml."""
    config_path = Path(__file__).parent / "config.yaml"
    if not config_path.exists():
        print("[!] config.yaml not found. Copy config.example.yaml to config.yaml first.")
        sys.exit(1)
    with open(config_path) as f:
        return yaml.safe_load(f)


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

        for article in articles:
            if not article_exists(article["id"]):
                save_article(article)
                new_count += 1

        print(f"    Found {len(articles)} items, {new_count} new")

    # Process any URLs queued via email replies
    manual_items = get_pending_manual_items()
    if manual_items:
        print(f"  Processing {len(manual_items)} manually queued URL(s)...")
    for item in manual_items:
        url = item["url"]
        print(f"    Fetching: {url}")
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
            if not article_exists(article["id"]):
                save_article(article)
                new_count += 1
        mark_manual_processed(item["id"])

    return new_count


def stage_process(config: dict) -> int:
    """Process unprocessed articles through Claude API. Returns count processed."""
    articles = get_unprocessed_articles(limit=15)
    processed = 0

    if not articles:
        print("  No unprocessed articles to process.")
        return 0

    print(f"  Processing {len(articles)} articles through Claude...")

    for article in articles:
        print(f"    → {article['title'][:60]}...")
        result = process_article(article, config)

        if result:
            update_article_processing(
                article_id=article["id"],
                summary=result.get("summary", ""),
                takeaways=result.get("key_takeaways", []),
                tags=result.get("tags", []),
                relevance_score=result.get("relevance_score", 0.0),
                think_about_this=result.get("think_about_this", ""),
                core_concept=result.get("core_concept", ""),
                related_search_terms=result.get("related_search_terms", []),
            )
            # Store extra fields in the article dict for later use
            # (these are passed through to the curator and template)
            processed += 1
        else:
            # Mark as processed with low score so we don't retry forever
            update_article_processing(
                article_id=article["id"],
                summary="[Processing failed]",
                takeaways=[],
                tags=[],
                relevance_score=0.0,
            )

    return processed


def stage_curate_and_send(config: dict, dry_run: bool = False) -> bool:
    """Curate a digest and send it. Returns True if sent successfully."""

    # 1. Check for replies/feedback first
    print("  Checking for email replies...")
    actions = check_replies(config)
    if actions:
        print(f"  Processed {len(actions)} reply actions")

    # 2. Curate today's digest
    print("  Curating today's digest...")
    digest = curate_digest(config)
    if not digest:
        print("  [!] Not enough content to curate a digest. Run ingest + process first.")
        return False

    print(f"  Theme: {digest['theme']}")
    print(f"  Articles: {[a['title'][:40] for a in digest['articles']]}")

    # 3. Get callback questions for spaced repetition
    callbacks = get_callback_questions(config)
    print(f"  Callbacks: {len(callbacks)} due for review")

    # 4. Enrich articles with processed fields from the database
    # The curator returns raw DB rows; we need to add the LLM-generated fields
    for article in digest["articles"]:
        # Parse stored JSON fields
        if isinstance(article.get("key_takeaways"), str):
            try:
                article["key_takeaways"] = json.loads(article["key_takeaways"])
            except (json.JSONDecodeError, TypeError):
                article["key_takeaways"] = []

        if isinstance(article.get("tags"), str):
            try:
                article["tags"] = json.loads(article["tags"])
            except (json.JSONDecodeError, TypeError):
                article["tags"] = []

        if not article.get("think_about_this"):
            article["think_about_this"] = ""
        if not article.get("related_search_terms"):
            article["related_search_terms"] = digest.get("further_reading_queries", [])
        elif isinstance(article.get("related_search_terms"), str):
            try:
                article["related_search_terms"] = json.loads(article["related_search_terms"])
            except (json.JSONDecodeError, TypeError):
                article["related_search_terms"] = []

    # 5. Render email
    print("  Rendering email...")
    html = render_email(digest, callbacks, config)

    if dry_run:
        # Save to file for preview
        preview_path = Path(__file__).parent / "data" / "preview.html"
        with open(preview_path, "w") as f:
            f.write(html)
        print(f"  [DRY RUN] Preview saved to {preview_path}")
        print(f"  Open in browser: file://{preview_path.resolve()}")
        return True

    # 6. Send
    print("  Sending digest...")
    success = send_email(html, digest)

    if success:
        # 7. Record the digest and advance repetitions
        article_ids = [a["id"] for a in digest["articles"]]
        save_digest(digest["theme"], digest.get("theme_description", ""), article_ids)

        create_repetitions_for_digest(digest["articles"], config)

        callback_ids = [cb["repetition_id"] for cb in callbacks]
        if callback_ids:
            mark_callbacks_shown(callback_ids, config)

        print("  [✓] Digest pipeline complete!")

    return success


# ---- Entry point ----

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "digest"

    print(f"\n{'='*50}")
    print(f"  Daily Digest — {mode.upper()} mode")
    print(f"{'='*50}\n")

    # Initialize database
    init_db()

    # Load config
    config = load_config()

    if mode == "ingest":
        count = stage_ingest(config)
        print(f"\n  Done. {count} new articles ingested.")

    elif mode == "process":
        count = stage_process(config)
        print(f"\n  Done. {count} articles processed.")

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

    else:
        print(f"Unknown mode: {mode}")
        print("Usage: python main.py [digest|ingest|process|test|replies]")
        sys.exit(1)


if __name__ == "__main__":
    main()
