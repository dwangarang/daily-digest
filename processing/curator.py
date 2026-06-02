"""
Curator: selects which articles appear in each digest.

Handles:
- Thematic coherence: finds a connecting thread among items
- Topic balancing: prevents any topic from dominating over time
- Feedback loop: reader signals influence selection
- Source dedup (hard): max 1 article per source before passing to Claude
- Framework diversity (hard): max 2 articles using the same think_framework per digest
"""

import os
import json
from collections import Counter
from anthropic import Anthropic
from data.db import get_unsent_articles, get_recent_digest_topics, get_recent_feedback

client = None


def _get_client():
    global client
    if client is None:
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return client


def _dedup_by_source(candidates: list) -> list:
    """
    Keep only the highest-relevance article per source_name.
    Manual items ([Manual ...]) are always kept — each is a distinct concept or URL.
    Candidates are assumed sorted by relevance_score DESC so the first occurrence wins.
    """
    seen = set()
    result = []
    for a in candidates:
        source = a.get("source_name", "")
        if source.startswith("[Manual"):
            result.append(a)
        elif source not in seen:
            seen.add(source)
            result.append(a)
    return result


def _enforce_framework_diversity(selected: list, pool: list, max_same: int = 2) -> list:
    """
    Ensure no more than max_same articles in the digest use the same think_framework.
    If a violation is found, swap the offending article for the best unused candidate
    from the pool that doesn't repeat the saturated framework.
    """
    fw_counts = Counter()
    result = []
    to_replace = []

    for article in selected:
        fw = article.get("think_framework") or "unset"
        if fw_counts[fw] < max_same:
            fw_counts[fw] += 1
            result.append(article)
        else:
            to_replace.append(article)

    if not to_replace:
        return result

    selected_ids = {a["id"] for a in selected}
    for article in to_replace:
        fw_out = article.get("think_framework") or "unset"
        replaced = False
        for candidate in pool:
            if candidate["id"] in selected_ids:
                continue
            fw_in = candidate.get("think_framework") or "unset"
            if fw_counts[fw_in] < max_same:
                fw_counts[fw_in] += 1
                result.append(candidate)
                selected_ids.add(candidate["id"])
                print(f"  [Framework dedup] '{article['title'][:45]}' ({fw_out}) "
                      f"→ '{candidate['title'][:45]}' ({fw_in})")
                replaced = True
                break
        if not replaced:
            # No valid replacement found — keep original rather than shrink the digest
            result.append(article)

    return result


def curate_digest(config: dict) -> dict | None:
    """
    Select articles for today's digest.

    Returns a dict with:
        theme: str
        theme_description: str
        articles: list[dict] — in presentation order
        further_reading_queries: list[str]

    Returns None if not enough content is available.
    """
    items_per_digest = config.get("schedule", {}).get("items_per_digest", 4)
    topics = config.get("topics", [])

    raw_candidates = get_unsent_articles(limit=60)
    # Hard dedup: one article per source before anything else
    candidates = _dedup_by_source(raw_candidates)

    if len(candidates) < items_per_digest:
        print(f"  [!] Only {len(candidates)} unique-source candidates. Need {items_per_digest}.")
        if len(candidates) < 2:
            return None

    recent_topics = get_recent_digest_topics(days=5)
    recent_feedback = get_recent_feedback(days=14)

    candidate_summaries = []
    for i, a in enumerate(candidates[:40]):  # cap at 40 for prompt size
        tags = json.loads(a.get("tags", "[]")) if a.get("tags") else []
        candidate_summaries.append({
            "index": i,
            "id": a["id"],
            "title": a["title"],
            "source": a["source_name"],
            "insight": a.get("insight") or a.get("summary", ""),
            "tags": tags,
            "relevance_score": a.get("relevance_score", 0),
            "think_framework": a.get("think_framework", ""),
        })

    topic_weights = {t["name"]: t["weight"] for t in topics}

    system_text = """You are curating a daily learning digest. Select articles that share a connecting thread — a common theme, tension, or question.

SELECTION CRITERIA (priority order):
1. THEMATIC COHERENCE: articles share a connecting thread. Most important.
2. TOPIC BALANCE: avoid topics that dominated recent digests.
3. READER FEEDBACK: honor explicit more/less signals.
4. RELEVANCE: prefer higher relevance_score, all else equal.
5. FRAMEWORK DIVERSITY: prefer selections where think_framework values vary. Max 2 articles may share the same framework.

HARD RULES:
- Each candidate already represents a unique source (pre-deduplicated). Do not worry about source diversity.
- Never select two candidates with the same think_framework value unless no valid alternative exists.

Return a JSON object (no markdown fences, no other text):
{
    "theme": "Short title for today's connecting thread (3-6 words)",
    "theme_description": "1-2 sentences explaining how these articles connect",
    "selected_indices": [0, 5, 12, 3],
    "presentation_order": [5, 0, 3, 12]
}

Rules:
- selected_indices: index values from the candidates list
- presentation_order: same indices reordered for narrative flow (reads top-to-bottom as a coherent arc)"""

    user_text = f"""Select exactly {items_per_digest} articles:

CANDIDATES:
{json.dumps(candidate_summaries, indent=2)}

TOPIC WEIGHT TARGETS: {json.dumps(topic_weights)}

TOPICS IN RECENT DIGESTS (avoid repeating):
{json.dumps(recent_topics)}

RECENT READER FEEDBACK:
{json.dumps(recent_feedback) if recent_feedback else "None yet."}"""

    try:
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        response = _get_client().messages.create(
            model=model,
            max_tokens=600,
            system=[{"type": "text", "text": system_text,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_text}],
        )

        raw_text = response.content[0].text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[1]
        if raw_text.endswith("```"):
            raw_text = raw_text.rsplit("```", 1)[0]

        selection = json.loads(raw_text.strip())
        ordered_articles = [
            candidates[idx]
            for idx in selection.get("presentation_order", selection["selected_indices"])
            if 0 <= idx < len(candidates)
        ]

        # Hard enforcement: framework diversity
        ordered_articles = _enforce_framework_diversity(ordered_articles, candidates)

        return {
            "theme": selection["theme"],
            "theme_description": selection.get("theme_description", ""),
            "articles": ordered_articles,
            "further_reading_queries": [],
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
