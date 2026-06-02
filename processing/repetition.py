"""
Spaced repetition engine.

After each digest is sent, this module:
1. Creates repetition entries for key concepts from today's articles
2. Retrieves due callback questions for inclusion in the next digest
3. Advances review intervals when concepts are shown again

The repetition model is concept-centric, not flashcard-centric:
- Early exposures: accessible summary + "think about this" question
- Later repetitions: synthesis questions connecting multiple concepts
- Progressive difficulty: each review asks a harder question
"""

import os
import json
from datetime import date, timedelta
from anthropic import Anthropic
from data.db import (
    save_repetition, get_due_repetitions, advance_repetition
)

client = None


def _get_client():
    global client
    if client is None:
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return client


def create_repetitions_for_digest(articles: list, config: dict):
    """
    Create spaced repetition entries for the articles in today's digest.

    Each article gets one core concept tracked for repetition.
    The first review is scheduled according to the first interval in config.
    """
    rep_config = config.get("repetition", {})
    if not rep_config.get("enabled", True):
        return

    intervals = rep_config.get("intervals", [1, 3, 7, 14, 30, 60])
    first_interval = intervals[0] if intervals else 1

    for article in articles:
        # The summarizer already extracted core_concept — use it if available
        # Otherwise fall back to the first key takeaway
        summary = article.get("summary", "")
        takeaways = article.get("key_takeaways")
        if isinstance(takeaways, str):
            try:
                takeaways = json.loads(takeaways)
            except json.JSONDecodeError:
                takeaways = []

        think_question = article.get("think_about_this", "")

        # Use the core concept as the tracked concept
        concept = article.get("core_concept", "")
        if not concept and takeaways:
            concept = takeaways[0]
        if not concept:
            concept = summary[:200] if summary else article.get("title", "")

        # Use the think_about_this question as the first review question
        question = think_question or f"What was the key insight from '{article.get('title', '')}'?"

        next_review = date.today() + timedelta(days=first_interval)

        save_repetition(
            article_id=article.get("id", ""),
            concept=concept,
            question=question,
            next_review_date=next_review,
        )


def get_callback_questions(config: dict) -> list:
    """
    Get callback questions due for today's digest.

    Returns a list of dicts with: question, concept, article_title, article_url,
    difficulty_level, repetition_id
    """
    rep_config = config.get("repetition", {})
    if not rep_config.get("enabled", True):
        return []

    max_callbacks = rep_config.get("callbacks_per_digest", 2)
    due = get_due_repetitions(limit=max_callbacks)

    callbacks = []
    for rep in due:
        # For higher difficulty levels, generate a harder question
        if rep["review_count"] >= 2:
            harder_q = _generate_synthesis_question(rep, config)
            if harder_q:
                question = harder_q
            else:
                question = rep["question"]
        else:
            question = rep["question"]

        callbacks.append({
            "question": question,
            "concept": rep["concept"],
            "article_title": rep.get("title", ""),
            "article_url": rep.get("url", ""),
            "difficulty_level": rep["difficulty_level"],
            "repetition_id": rep["id"],
        })

    return callbacks


def mark_callbacks_shown(callback_ids: list, config: dict):
    """Advance repetition intervals for callbacks that were shown in a digest."""
    intervals = config.get("repetition", {}).get("intervals", [1, 3, 7, 14, 30, 60])
    for rep_id in callback_ids:
        advance_repetition(rep_id, intervals)


def _generate_synthesis_question(rep: dict, config: dict) -> str | None:
    """
    Generate a harder synthesis question for later repetition stages.

    Connects the original concept to the reader's broader interests.
    """
    try:
        interest_profile = config.get("interest_profile", "")
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

        prompt = f"""Generate one synthesis question for spaced repetition review.

ORIGINAL CONCEPT: {rep['concept']}
ORIGINAL ARTICLE: {rep.get('title', '')}
REVIEW COUNT: {rep['review_count']} (this is review #{rep['review_count'] + 1})
READER PROFILE: {interest_profile}

The question should:
- Require APPLYING the concept to a new context, not just recalling it
- Connect to the reader's professional interests where possible
- Be harder than the previous review question
- Be answerable in 2-3 sentences of thinking (not a research project)

Return ONLY the question text, nothing else."""

        response = _get_client().messages.create(
            model=model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()

    except Exception as e:
        print(f"  [!] Failed to generate synthesis question: {e}")
        return None
