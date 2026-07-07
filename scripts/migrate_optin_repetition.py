"""
One-time migration to the opt-in spaced-repetition model (2026-07).

What it does, in order:
1. Applies schema migrations (repetitions.answer/retired, articles.priority,
   digest_callbacks table) via init_db().
2. Deletes every repetition whose article is NOT from the Expert Roster —
   the old model auto-enrolled all digest articles and bulk ingests (104
   Howard Marks memos flooded the queue). Those articles stay in the content
   pool; they re-enter recall only when the reader 👍s them in a digest.
3. Regenerates each kept expert entry as an atomic Q&A card (the old entries
   were open-ended essay prompts with no answer), resets its interval state,
   and staggers due dates at callbacks_per_digest/day starting tomorrow so
   the queue drains evenly instead of all being due at once.

Requires ANTHROPIC_API_KEY (card regeneration). Safe to rerun.

Usage: python scripts/migrate_optin_repetition.py [--db path/to/digest.db]
"""

import sys
import argparse
from pathlib import Path
from datetime import date, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

import data.db as db
from processing.repetition import generate_card


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None, help="Path to digest.db (default: data/digest.db)")
    args = parser.parse_args()

    if args.db:
        db.DB_PATH = str(Path(args.db).resolve())
    print(f"Migrating: {db.DB_PATH}\n")

    db.init_db()

    config_path = Path(__file__).parent.parent / "config.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)
    callbacks_per_digest = config.get("repetition", {}).get("callbacks_per_digest", 2)

    conn = db.get_connection()

    total = conn.execute("SELECT COUNT(*) FROM repetitions").fetchone()[0]
    cur = conn.execute("""
        DELETE FROM repetitions WHERE article_id NOT IN
        (SELECT id FROM articles WHERE source_name = 'Expert Roster')
    """)
    deleted = cur.rowcount
    conn.commit()

    experts = conn.execute("""
        SELECT r.id AS rep_id, r.article_id, a.title, a.core_concept, a.insight,
               a.summary, a.key_takeaways, r.answer
        FROM repetitions r JOIN articles a ON r.article_id = a.id
        ORDER BY r.id
    """).fetchall()
    conn.close()

    print(f"Repetitions: {total} total → deleted {deleted} non-expert, kept {len(experts)} expert entries.\n")

    regenerated = 0
    for i, rep in enumerate(experts):
        rep = dict(rep)
        # Stagger: callbacks_per_digest cards become due per day, starting tomorrow.
        due = date.today() + timedelta(days=1 + i // callbacks_per_digest)

        card = None
        if not rep.get("answer"):
            concept = rep.get("core_concept") or rep.get("insight") or rep.get("title", "")
            print(f"  Generating card: {rep['title']}...")
            card = generate_card(
                concept=concept,
                insight=rep.get("insight") or rep.get("summary", ""),
                takeaways=rep.get("key_takeaways") or [],
                title=rep.get("title", ""),
                config=config,
            )

        conn = db.get_connection()
        if card:
            conn.execute("""
                UPDATE repetitions
                SET question = ?, answer = ?, review_count = 0, difficulty_level = 1,
                    retired = 0, next_review_date = ?
                WHERE id = ?
            """, (card["question"], card["answer"], due.isoformat(), rep["rep_id"]))
            regenerated += 1
        else:
            conn.execute(
                "UPDATE repetitions SET next_review_date = ? WHERE id = ?",
                (due.isoformat(), rep["rep_id"]),
            )
        conn.commit()
        conn.close()

    print(f"\nDone. {regenerated} expert entries regenerated as Q&A cards, "
          f"due dates staggered over ~{max(1, len(experts) // callbacks_per_digest)} days.")


if __name__ == "__main__":
    main()
