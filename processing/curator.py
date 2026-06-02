"""
Curator: selects which articles appear in each digest.

Handles:
- Thematic coherence: finds a connecting thread among items
- Topic balancing: prevents any topic from dominating over time
- Freshness vs. depth: mixes recent and evergreen content
- Feedback loop: reader signals (more/less, topic adjustments) influence selection
"""

import os
import json
from anthropic import Anthropic
from data.db import get_unsent_articles, get_recent_digest_topics, get_recent_feedback

client = None


def _get_client():
    global client
    if client is None:
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return client


def curate_digest(config: dict) -> dict | None:
    """
    Select articles for today's digest.

    Returns a dict with:
        theme: str - the connecting thread
        theme_description: str - 1-2 sentence description of the theme
        articles: list[dict] - selected articles in presentation order
        further_reading_queries: list[str]

    Returns None if not enough content is available.
    """
    items_per_digest = config.get("schedule", {}).get("items_per_digest", 4)
    topics = config.get("topics", [])

    candidates = get_unsent_articles(limit=40)
    if len(candidates) < items_per_digest:
        print(f"  [!] Only {len(candidates)} unsent articles. Need {items_per_digest}.")
        if len(candidates) < 2:
            return None

    recent_topics = get_recent_digest_topics(days=5)
    recent_feedback = get_recent_feedback(days=14)

    candidate_summaries = []
    for i, a in enumerate(candidates):
        tags = json.loads(a.get("tags", "[]")) if a.get("tags") else []
        candidate_summaries.append({
            "index": i,
            "id": a["id"],
            "title": a["title"],
            "source": a["source_name"],
            "summary": a.get("summary", ""),
            "tags": tags,
            "relevance_score": a.get("relevance_score", 0),
        })

    topic_weights = {t["name"]: t["weight"] for t in topics}

    system_text = """You are curating a daily learning digest. Your job is to select articles that share a connecting thread — a common theme, tension, or question that links them.

SELECTION CRITERIA (in priority order):
1. THEMATIC COHERENCE: The selected articles should share a connecting thread. This is the most important criterion.
2. TOPIC BALANCE: Avoid topics that dominated recent digests. Underrepresented topics get priority.
3. READER FEEDBACK: Honor explicit signals — if the reader said "less X" or "more Y", apply that.
4. RELEVANCE: Higher relevance_score articles preferred, all else equal.
5. SOURCE DIVERSITY: Avoid picking multiple articles from the same source.

Return a JSON object (no markdown fences, no other text):
{
    "theme": "A short title for today's connecting thread (3-6 words)",
    "theme_description": "1-2 sentences explaining how these articles connect",
    "selected_indices": [0, 5, 12, 3],
    "presentation_order": [5, 0, 3, 12],
    "further_reading_queries": ["search term 1", "search term 2", "search term 3"]
}

Rules:
- selected_indices: the index values from the candidates
- presentation_order: the same indices, reordered for narrative flow (the email reads top-to-bottom as a coherent arc)
- further_reading_queries: 3 search terms someone could use to explore today's theme further"""

    user_text = f"""Select exactly {items_per_digest} articles from these candidates:

CANDIDATES:
{json.dumps(candidate_summaries, indent=2)}

TOPIC WEIGHT TARGETS: {json.dumps(topic_weights)}

TOPICS COVERED IN RECENT DIGESTS (last 5 days, avoid repeating these):
{json.dumps(recent_topics)}

RECENT READER FEEDBACK (honor these signals in your selection):
{json.dumps(recent_feedback) if recent_feedback else "No feedback yet."}"""

    try:
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        response = _get_client().messages.create(
            model=model,
            max_tokens=800,
            system=[
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_text}],
        )

        raw_text = response.content[0].text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[1]
        if raw_text.endswith("```"):
            raw_text = raw_text.rsplit("```", 1)[0]
        raw_text = raw_text.strip()

        selection = json.loads(raw_text)

        ordered_articles = []
        for idx in selection.get("presentation_order", selection["selected_indices"]):
            if 0 <= idx < len(candidates):
                ordered_articles.append(candidates[idx])

        return {
            "theme": selection["theme"],
            "theme_description": selection.get("theme_description", ""),
            "articles": ordered_articles,
            "further_reading_queries": selection.get("further_reading_queries", []),
        }

    except Exception as e:
        print(f"  [!] Curation failed: {e}")
        fallback = sorted(candidates, key=lambda a: a.get("relevance_score", 0),
                          reverse=True)[:items_per_digest]
        return {
            "theme": "Today's Top Reads",
            "theme_description": "Selected by relevance score.",
            "articles": fallback,
            "further_reading_queries": [],
        }
