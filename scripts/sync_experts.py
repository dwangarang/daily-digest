"""
Sync the expert roster (config.yaml's `experts` list) into the spaced-repetition
pipeline, the same way a manually added concept or an email-reply concept is
processed. The experts already drive digest-time lens analysis via config.yaml
directly — this only adds them to the review/recall loop.

Safe to rerun: skips any expert name that already has an article record.

Usage: python scripts/sync_experts.py
"""

import sys
import hashlib
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from data.db import get_connection, save_article, update_article_processing, init_db
from processing.summarizer import process_concept
from processing.repetition import create_repetitions_for_digest


def already_synced(name: str) -> bool:
    conn = get_connection()
    row = conn.execute(
        "SELECT 1 FROM articles WHERE source_name = 'Expert Roster' AND title = ? LIMIT 1",
        (name,),
    ).fetchone()
    conn.close()
    return row is not None


def main():
    init_db()
    config_path = Path(__file__).parent.parent / "config.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    experts = config.get("experts", [])
    if not experts:
        print("No experts found in config.yaml.")
        return

    added = 0
    for expert in experts:
        name = expert["name"]
        if already_synced(name):
            print(f"  [skip] {name} already synced")
            continue

        topic = (expert.get("domains") or ["General Interest"])[0]
        print(f"  Processing {name}...")
        result = process_concept(name, expert["framework"], topic, config)
        if not result:
            print(f"  [!] Failed to process {name}, skipping")
            continue

        article_id = hashlib.sha256(
            f"expert:{name}:{datetime.now().isoformat()}".encode()
        ).hexdigest()[:16]

        save_article({
            "id": article_id, "source_name": "Expert Roster",
            "title": name, "url": "", "raw_content": expert["framework"],
        })
        update_article_processing(
            article_id=article_id,
            summary=result.get("insight", ""),
            takeaways=result.get("key_takeaways", []),
            tags=result.get("tags", []),
            relevance_score=result.get("relevance_score", 0.0),
            think_about_this=result.get("think_about_this", ""),
            core_concept=result.get("core_concept", name),
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
            "id": article_id, "title": name, "url": "",
            "summary": result.get("insight", ""),
            "key_takeaways": result.get("key_takeaways", []),
            "think_about_this": result.get("think_about_this", ""),
            "core_concept": result.get("core_concept", name),
        }], config)
        print(f"  [added] {name} ({topic})")
        added += 1

    print(f"\n{added} expert(s) added to the spaced-repetition pool.")


if __name__ == "__main__":
    main()
