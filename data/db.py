"""
Database layer for Daily Digest.

Uses SQLite to track all state: ingested articles, sent digests,
spaced repetition schedule, user feedback, manual URL queue, and concept queue.
"""

import sqlite3
import json
import os
from datetime import datetime, date

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "digest.db")


def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS articles (
            id TEXT PRIMARY KEY,
            source_name TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT,
            raw_content TEXT,
            summary TEXT,
            key_takeaways TEXT,
            tags TEXT,
            relevance_score REAL DEFAULT 0.0,
            think_about_this TEXT,
            core_concept TEXT,
            related_search_terms TEXT,
            insight TEXT,
            so_what TEXT,
            contrarian_angle TEXT,
            further_reading TEXT,
            think_framework TEXT,
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processed_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS digests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            theme TEXT,
            theme_description TEXT,
            article_ids TEXT
        );

        CREATE TABLE IF NOT EXISTS digest_articles (
            digest_id INTEGER,
            article_id TEXT,
            position INTEGER,
            FOREIGN KEY (digest_id) REFERENCES digests(id),
            FOREIGN KEY (article_id) REFERENCES articles(id),
            PRIMARY KEY (digest_id, article_id)
        );

        CREATE TABLE IF NOT EXISTS repetitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id TEXT NOT NULL,
            concept TEXT NOT NULL,
            question TEXT NOT NULL,
            difficulty_level INTEGER DEFAULT 1,
            next_review_date DATE NOT NULL,
            last_reviewed_date DATE,
            review_count INTEGER DEFAULT 0,
            FOREIGN KEY (article_id) REFERENCES articles(id)
        );

        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id TEXT,
            signal TEXT NOT NULL,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (article_id) REFERENCES articles(id)
        );

        CREATE TABLE IF NOT EXISTS topic_preferences (
            topic TEXT PRIMARY KEY,
            weight REAL NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS manual_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            note TEXT,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processed BOOLEAN DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS concept_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            explanation TEXT NOT NULL,
            topic TEXT NOT NULL,
            source TEXT,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processed BOOLEAN DEFAULT 0
        );
    """)

    # Migrations: add new columns to existing databases
    for col, definition in [
        ("think_about_this", "TEXT"),
        ("core_concept", "TEXT"),
        ("related_search_terms", "TEXT"),
        ("insight", "TEXT"),
        ("so_what", "TEXT"),
        ("contrarian_angle", "TEXT"),
        ("further_reading", "TEXT"),
        ("think_framework", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass  # Column already exists

    conn.commit()
    conn.close()


# ---- Article helpers ----

def article_exists(article_id: str) -> bool:
    conn = get_connection()
    row = conn.execute("SELECT 1 FROM articles WHERE id = ?", (article_id,)).fetchone()
    conn.close()
    return row is not None


def save_article(article: dict):
    conn = get_connection()
    conn.execute("""
        INSERT OR IGNORE INTO articles (id, source_name, title, url, raw_content, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        article["id"],
        article["source_name"],
        article["title"],
        article.get("url"),
        article.get("raw_content", ""),
        datetime.now().isoformat()
    ))
    conn.commit()
    conn.close()


def update_article_processing(article_id: str, summary: str, takeaways: list,
                               tags: list, relevance_score: float,
                               think_about_this: str = "", core_concept: str = "",
                               related_search_terms: list = None,
                               insight: str = "", so_what: str = "",
                               contrarian_angle: str = "",
                               further_reading: list = None,
                               think_framework: str = ""):
    conn = get_connection()
    conn.execute("""
        UPDATE articles
        SET summary = ?, key_takeaways = ?, tags = ?, relevance_score = ?,
            think_about_this = ?, core_concept = ?, related_search_terms = ?,
            insight = ?, so_what = ?, contrarian_angle = ?, further_reading = ?,
            think_framework = ?, processed_at = ?
        WHERE id = ?
    """, (
        summary,
        json.dumps(takeaways),
        json.dumps(tags),
        relevance_score,
        think_about_this,
        core_concept,
        json.dumps(related_search_terms or []),
        insight,
        so_what,
        contrarian_angle,
        json.dumps(further_reading or []),
        think_framework,
        datetime.now().isoformat(),
        article_id
    ))
    conn.commit()
    conn.close()


def get_unsent_articles(limit: int = 50) -> list:
    conn = get_connection()
    rows = conn.execute("""
        SELECT a.* FROM articles a
        WHERE a.processed_at IS NOT NULL
        AND a.id NOT IN (SELECT article_id FROM digest_articles)
        ORDER BY a.relevance_score DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_unprocessed_articles(limit: int = 20) -> list:
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM articles
        WHERE processed_at IS NULL
        ORDER BY ingested_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---- Digest helpers ----

def save_digest(theme: str, theme_description: str, article_ids: list) -> int:
    conn = get_connection()
    cursor = conn.execute("""
        INSERT INTO digests (sent_at, theme, theme_description, article_ids)
        VALUES (?, ?, ?, ?)
    """, (datetime.now().isoformat(), theme, theme_description, json.dumps(article_ids)))
    digest_id = cursor.lastrowid
    for i, aid in enumerate(article_ids):
        conn.execute("""
            INSERT INTO digest_articles (digest_id, article_id, position)
            VALUES (?, ?, ?)
        """, (digest_id, aid, i + 1))
    conn.commit()
    conn.close()
    return digest_id


def get_recent_digest_topics(days: int = 7) -> list:
    conn = get_connection()
    rows = conn.execute("""
        SELECT a.tags FROM articles a
        JOIN digest_articles da ON a.id = da.article_id
        JOIN digests d ON da.digest_id = d.id
        WHERE d.sent_at > datetime('now', ?)
    """, (f"-{days} days",)).fetchall()
    conn.close()
    all_tags = []
    for r in rows:
        if r["tags"]:
            all_tags.extend(json.loads(r["tags"]))
    return all_tags


def get_most_recent_digest_articles() -> list:
    """Return [(position, article_id), ...] for the most recently sent digest."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT da.position, da.article_id FROM digest_articles da
        JOIN digests d ON da.digest_id = d.id
        WHERE d.id = (SELECT id FROM digests ORDER BY sent_at DESC LIMIT 1)
        ORDER BY da.position
    """).fetchall()
    conn.close()
    return [(r["position"], r["article_id"]) for r in rows]


# ---- Repetition helpers ----

def save_repetition(article_id: str, concept: str, question: str,
                     next_review_date: date):
    conn = get_connection()
    conn.execute("""
        INSERT INTO repetitions (article_id, concept, question, next_review_date)
        VALUES (?, ?, ?, ?)
    """, (article_id, concept, question, next_review_date.isoformat()))
    conn.commit()
    conn.close()


def get_due_repetitions(as_of: date = None, limit: int = 3) -> list:
    if as_of is None:
        as_of = date.today()
    conn = get_connection()
    rows = conn.execute("""
        SELECT r.*, a.title, a.summary, a.url FROM repetitions r
        JOIN articles a ON r.article_id = a.id
        WHERE r.next_review_date <= ?
        ORDER BY r.next_review_date ASC, r.review_count ASC
        LIMIT ?
    """, (as_of.isoformat(), limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def advance_repetition(repetition_id: int, intervals: list):
    conn = get_connection()
    row = conn.execute(
        "SELECT review_count FROM repetitions WHERE id = ?", (repetition_id,)
    ).fetchone()
    if row:
        count = row["review_count"]
        next_idx = min(count, len(intervals) - 1)
        next_days = intervals[next_idx]
        conn.execute("""
            UPDATE repetitions
            SET review_count = review_count + 1,
                difficulty_level = difficulty_level + 1,
                last_reviewed_date = ?,
                next_review_date = date('now', ?)
            WHERE id = ?
        """, (date.today().isoformat(), f"+{next_days} days", repetition_id))
    conn.commit()
    conn.close()


# ---- Feedback helpers ----

def save_feedback(article_id: str, signal: str, details: str = None):
    conn = get_connection()
    conn.execute("""
        INSERT INTO feedback (article_id, signal, details)
        VALUES (?, ?, ?)
    """, (article_id, signal, details))
    conn.commit()
    conn.close()


def get_recent_feedback(days: int = 14) -> list:
    conn = get_connection()
    rows = conn.execute("""
        SELECT signal, details, created_at FROM feedback
        WHERE created_at > datetime('now', ?)
        ORDER BY created_at DESC
        LIMIT 20
    """, (f"-{days} days",)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---- Manual URL queue helpers ----

def add_to_manual_queue(url: str, note: str = None):
    conn = get_connection()
    conn.execute("INSERT INTO manual_queue (url, note) VALUES (?, ?)", (url, note))
    conn.commit()
    conn.close()


def get_pending_manual_items() -> list:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM manual_queue WHERE processed = 0 ORDER BY added_at ASC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_manual_processed(item_id: int):
    conn = get_connection()
    conn.execute("UPDATE manual_queue SET processed = 1 WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()


# ---- Concept queue helpers ----

def add_to_concept_queue(name: str, explanation: str, topic: str, source: str = None):
    conn = get_connection()
    conn.execute("""
        INSERT INTO concept_queue (name, explanation, topic, source)
        VALUES (?, ?, ?, ?)
    """, (name, explanation, topic, source))
    conn.commit()
    conn.close()


def get_pending_concepts() -> list:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM concept_queue WHERE processed = 0 ORDER BY added_at ASC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_concept_processed(item_id: int):
    conn = get_connection()
    conn.execute("UPDATE concept_queue SET processed = 1 WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
