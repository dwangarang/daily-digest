"""
Spaced repetition engine — opt-in, atomic Q&A cards.

Nothing enters the queue automatically. An article gets a card only when the
reader signals they want to retain it:
- 👍 on a digest item ("more item N" reply) → enroll_article()
- explicitly added concepts (add-concept mode, "concept:" replies, expert sync)

Each card is an atomic question → answer pair (concept → mechanism/definition),
generated once at enrollment. The recall loop is honest spaced repetition:
- the question appears in a digest's Recall Check; the answer prints at the
  bottom of the same email
- showing a card advances its interval (silence = implicit pass)
- reply "recall N missed" resets it to the shortest interval
- reply "stop recall N" (or 👎) retires it permanently
"""

import os
import json
from datetime import date, timedelta
from anthropic import Anthropic
from data.db import (
    save_repetition, get_due_repetitions, advance_repetition,
    repetition_exists, retire_repetition, reset_repetition, get_article,
)

client = None


def _get_client():
    global client
    if client is None:
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return client


def generate_card(concept: str, insight: str, takeaways: list, title: str,
                  config: dict) -> dict | None:
    """
    Generate one atomic Q&A card for a concept. Returns {question, answer} or None.

    The card must be self-gradable: a factual question about the concept's
    mechanism or definition, not an open-ended reflection prompt.
    """
    if isinstance(takeaways, str):
        try:
            takeaways = json.loads(takeaways)
        except json.JSONDecodeError:
            takeaways = []

    prompt = f"""Create ONE atomic spaced-repetition card for this concept.

CONCEPT: {concept}
FROM: {title}
CORE INSIGHT: {insight}
KEY TAKEAWAYS: {json.dumps(takeaways[:3])}

Requirements:
- The question asks for a specific mechanism, definition, or causal relationship
  — something with a right answer the reader can check themselves against.
- NOT an essay prompt, NOT "reflect on...", NOT "how would you apply..."
- The answer is 1-3 sentences: the precise mechanism/definition being tested.
- Question and answer must stand alone without the source article.

Return ONLY a JSON object: {{"question": "...", "answer": "..."}}"""

    try:
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        response = _get_client().messages.create(
            model=model,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        card = json.loads(raw.strip())
        if card.get("question") and card.get("answer"):
            return card
        return None
    except Exception as e:
        print(f"  [!] Card generation failed for '{concept[:50]}': {e}")
        return None


def enroll_article(article_id: str, config: dict) -> bool:
    """
    Enroll an article's core concept into the recall queue (reader gave 👍
    or explicitly added it). Generates an atomic Q&A card. Idempotent.
    """
    rep_config = config.get("repetition", {})
    if not rep_config.get("enabled", True):
        return False
    if repetition_exists(article_id):
        print(f"    → Already in recall queue: {article_id}")
        return False

    article = get_article(article_id)
    if not article:
        print(f"  [!] Cannot enroll unknown article {article_id}")
        return False

    concept = article.get("core_concept") or article.get("insight") \
        or article.get("summary", "")[:200] or article.get("title", "")

    card = generate_card(
        concept=concept,
        insight=article.get("insight") or article.get("summary", ""),
        takeaways=article.get("key_takeaways") or [],
        title=article.get("title", ""),
        config=config,
    )
    if not card:
        return False

    intervals = rep_config.get("intervals", [1, 3, 7, 14, 30, 60])
    next_review = date.today() + timedelta(days=intervals[0] if intervals else 1)

    save_repetition(
        article_id=article_id,
        concept=concept,
        question=card["question"],
        answer=card["answer"],
        next_review_date=next_review,
    )
    print(f"    → Enrolled in recall queue: {article.get('title', '')[:50]}")
    return True


def get_callback_questions(config: dict) -> list:
    """
    Get recall cards due for today's digest.

    Returns a list of dicts with: question, answer, concept, article_title,
    article_url, review_count, repetition_id.
    """
    rep_config = config.get("repetition", {})
    if not rep_config.get("enabled", True):
        return []

    max_callbacks = rep_config.get("callbacks_per_digest", 2)
    due = get_due_repetitions(limit=max_callbacks)

    return [{
        "question": rep["question"],
        "answer": rep.get("answer") or "",
        "concept": rep["concept"],
        "article_title": rep.get("title", ""),
        "article_url": rep.get("url", ""),
        "review_count": rep["review_count"],
        "repetition_id": rep["id"],
    } for rep in due]


def mark_callbacks_shown(callback_ids: list, config: dict):
    """
    Advance intervals for cards shown in a digest (silence = implicit pass).
    An explicit "recall N missed" reply later resets the card.
    """
    intervals = config.get("repetition", {}).get("intervals", [1, 3, 7, 14, 30, 60])
    for rep_id in callback_ids:
        advance_repetition(rep_id, intervals)


def grade_card(repetition_id: int, passed: bool, config: dict):
    """Apply an explicit reader grade to a resurfaced card."""
    if passed:
        # Interval already advanced when the card was shown — nothing to do.
        return
    intervals = config.get("repetition", {}).get("intervals", [1, 3, 7, 14, 30, 60])
    reset_repetition(repetition_id, intervals[0] if intervals else 1)


def drop_card(repetition_id: int):
    """Reader gave 👎 on a resurfaced card — retire it from the queue."""
    retire_repetition(repetition_id)
