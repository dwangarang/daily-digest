"""
One-time backfill: split data/the-complete-collection.pdf (Howard Marks' full
memo anthology, 1990-2025) into individual memos and ingest those from
YEAR_CUTOFF onward via the same path as `main.py add-file`.

oaktreecapital.com/insights is JS-rendered and can't be scraped directly (the
"Howard Marks Memos" evergreen source in config.yaml reliably saves 0 articles),
so this PDF is the actual ingestion route for that source.

Safe to rerun: skips any memo whose deterministic article_id already exists.

Usage: python scripts/ingest_howard_marks.py
"""

import sys
import re
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
import pdfplumber
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from data.db import article_exists, save_article, update_article_processing, init_db
from processing.summarizer import process_article
from processing.repetition import create_repetitions_for_digest

PDF_PATH = Path(__file__).parent.parent / "data" / "the-complete-collection.pdf"
YEAR_CUTOFF_ANCHOR_TITLE = "The Aviary"  # first memo at/after the 2006 cutoff
MAX_CHARS = 15000  # matches the truncation stage_add_file uses for PDF/text uploads
SOURCE_LABEL = "[File] Howard Marks"


def find_memo_starts(page_texts: list[str]) -> list[tuple[int, str]]:
    starts = []
    for i, text in enumerate(page_texts):
        if "Memo to:" not in text or "Re:" not in text:
            continue
        title = None
        for line in text.split("\n")[:8]:
            if line.strip().startswith("Re:"):
                title = line.strip()[3:].strip()
                break
        if title:
            starts.append((i, title))
    return starts


def main():
    init_db()
    config_path = Path(__file__).parent.parent / "config.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    print("Opening PDF (this parses all pages, takes a couple minutes)...")
    with pdfplumber.open(PDF_PATH) as pdf:
        page_texts = [page.extract_text() or "" for page in pdf.pages]

    starts = find_memo_starts(page_texts)
    anchor_idx = next(i for i, (_, title) in enumerate(starts) if title == YEAR_CUTOFF_ANCHOR_TITLE)
    memos = starts[anchor_idx:]
    print(f"{len(memos)} memos from '{YEAR_CUTOFF_ANCHOR_TITLE}' onward.\n")

    added, skipped, failed = 0, 0, 0
    for idx, (start_page, title) in enumerate(memos):
        end_page = memos[idx + 1][0] if idx + 1 < len(memos) else len(page_texts)
        text = "\n".join(page_texts[start_page:end_page])[:MAX_CHARS]

        article_id = hashlib.sha256(
            f"file:the-complete-collection.pdf:{title}".encode()
        ).hexdigest()[:16]

        if article_exists(article_id):
            print(f"  [skip] {title}")
            skipped += 1
            continue

        article = {
            "id": article_id, "source_name": SOURCE_LABEL,
            "title": title, "url": "", "raw_content": text,
        }
        save_article(article)

        print(f"  Processing: {title}...")
        result = process_article(article, config)
        if not result:
            print(f"  [!] Failed: {title}")
            failed += 1
            continue

        update_article_processing(
            article_id=article_id,
            summary=result.get("insight", ""),
            takeaways=result.get("key_takeaways", []),
            tags=result.get("tags", []),
            relevance_score=result.get("relevance_score", 0.0),
            think_about_this=result.get("think_about_this", ""),
            core_concept=result.get("core_concept", title),
            related_search_terms=[],
            insight=result.get("insight", ""),
            so_what=result.get("so_what", ""),
            contrarian_angle=result.get("contrarian_angle", ""),
            further_reading=result.get("further_reading", []),
            think_framework=result.get("think_framework", ""),
            historical_analog=result.get("historical_analog"),
            context=result.get("context", ""),
            takeaway=result.get("takeaway", ""),
        )
        create_repetitions_for_digest([{
            "id": article_id, "title": title, "url": "",
            "summary": result.get("insight", ""),
            "key_takeaways": result.get("key_takeaways", []),
            "think_about_this": result.get("think_about_this", ""),
            "core_concept": result.get("core_concept", title),
        }], config)
        added += 1

    print(f"\nDone. {added} added, {skipped} already present, {failed} failed.")


if __name__ == "__main__":
    main()
