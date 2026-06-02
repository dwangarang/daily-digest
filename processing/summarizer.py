"""
Summarization and analysis via Claude API.

Uses prompt caching: the interest profile and instructions are sent as a
cached system prompt, so repeated calls within a run only pay for the
article content itself (~80% cost reduction after the first call).
"""

import os
import json
from anthropic import Anthropic
from sources.scraper import fetch_full_article

client = None


def _get_client():
    global client
    if client is None:
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return client


def _parse_json_response(raw_text: str) -> dict:
    raw_text = raw_text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[1]
    if raw_text.endswith("```"):
        raw_text = raw_text.rsplit("```", 1)[0]
    return json.loads(raw_text.strip())


def process_article(article: dict, config: dict) -> dict | None:
    """
    Process a single article through Claude.

    Returns a dict with: insight, so_what, contrarian_angle, key_takeaways,
    tags, relevance_score, think_about_this, further_reading, core_concept.
    Returns None if processing fails.
    """
    content = article.get("raw_content", "")
    if not content or content.startswith("[External link:"):
        url = article.get("url", "")
        if url:
            content = fetch_full_article(url)
        if not content:
            print(f"  [!] No content available for: {article['title']}")
            return None

    interest_profile = config.get("interest_profile", "")
    topics = config.get("topics", [])
    topic_names = [t["name"] for t in topics]

    system_text = f"""You are a sharp research analyst processing articles for a specific reader. Your job is not to summarize — it is to extract what's non-obvious and frame it for someone with 4 years of strategy consulting experience and an MBA focused on AI, enterprise SaaS GTM, and China-ASEAN markets.

Reader profile:
{interest_profile}

Available topic tags: {json.dumps(topic_names)}

Return a JSON object with this exact structure (no markdown fences, no other text):
{{
    "insight": "The single non-obvious or contrarian takeaway. What would a smart person miss on first read? What's the second-order implication? Direct, opinionated voice. 2-3 sentences max.",
    "so_what": "One sentence connecting this insight to the reader's professional context — AI strategy, enterprise SaaS GTM, or China-ASEAN markets. Should feel like a sharp colleague saying 'here's why you should care.'",
    "contrarian_angle": "One sentence: the strongest counterargument to the article's thesis, or the thing most readers would get wrong about it.",
    "key_takeaways": ["takeaway 1", "takeaway 2", "takeaway 3"],
    "tags": ["tag1"],
    "relevance_score": 0.75,
    "think_about_this": "A cross-domain synthesis question. Hard requirements: (1) MUST explicitly connect this article's concept to a concept from a DIFFERENT domain — e.g. connect an AI governance paper to an investing mental model, or a GTM piece to a geopolitics dynamic; (2) must present a SPECIFIC CLAIM the reader evaluates — not 'how would you approach X' but 'claim X implies Y — do you agree?'; (3) difficulty calibrated for 4 years strategy consulting + MBA.",
    "further_reading": [
        {{"title": "Specific real article, essay, book chapter, or paper title", "author": "Author name, or empty string if unknown", "reason": "One sentence on why this specific piece adds a genuinely different angle — not just 'also related to this topic'"}},
        {{"title": "...", "author": "...", "reason": "..."}},
        {{"title": "...", "author": "...", "reason": "..."}}
    ],
    "core_concept": "The single most important idea from this article in one sentence. Used for spaced repetition — write it as a standalone insight, not a reference to the article."
}}

Rules:
- insight: opinionated, not neutral. What a second-order thinker notices that a casual reader misses.
- so_what: must name a specific domain the reader cares about (not generic "business" or "strategy")
- contrarian_angle: steelman the opposing view or surface the common misreading
- tags: pick 1-2 from the available topic tags only
- relevance_score: 0.0–1.0 based on match with reader profile
- think_about_this: must be cross-domain, must make a specific claim, must not be yes/no answerable
- further_reading: real titles with specificity — "Howard Marks' 'The Most Important Thing'" beats "books about investing risk"
- core_concept: a standalone insight, not "the article argues that...""""

    user_text = f"""Analyze this article:

TITLE: {article['title']}
SOURCE: {article['source_name']}

CONTENT:
{content[:8000]}"""

    try:
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        response = _get_client().messages.create(
            model=model,
            max_tokens=1200,
            system=[{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_text}],
        )
        return _parse_json_response(response.content[0].text)

    except json.JSONDecodeError as e:
        print(f"  [!] Failed to parse Claude response for '{article['title']}': {e}")
        return None
    except Exception as e:
        print(f"  [!] Claude API error for '{article['title']}': {e}")
        return None


def process_concept(name: str, explanation: str, topic: str, config: dict) -> dict | None:
    """
    Process a manually entered concept through Claude.

    Takes the user's own explanation and enriches it with insight, so_what,
    contrarian_angle, think_about_this, further_reading, and core_concept.
    Returns None if processing fails.
    """
    interest_profile = config.get("interest_profile", "")
    topics = config.get("topics", [])
    topic_names = [t["name"] for t in topics]

    system_text = f"""You are a sharp research analyst helping a reader deepen their understanding of a concept they already know. Your job is to add the non-obvious angle and connect it to their professional context.

Reader profile:
{interest_profile}

Available topic tags: {json.dumps(topic_names)}

The reader will provide a concept name and their own explanation. Return a JSON object with this exact structure (no markdown fences, no other text):
{{
    "insight": "The non-obvious angle on this concept. What would a second-order thinker add to the reader's explanation? What implication or edge case does it point to? 2-3 sentences, direct and opinionated.",
    "so_what": "One sentence connecting this concept to the reader's professional context — AI strategy, SaaS GTM, or China-ASEAN markets.",
    "contrarian_angle": "One sentence: the strongest objection to this concept, or the most common way practitioners misapply it.",
    "key_takeaways": ["takeaway 1", "takeaway 2", "takeaway 3"],
    "tags": ["tag from available list"],
    "relevance_score": 0.85,
    "think_about_this": "A cross-domain synthesis question. Hard requirements: (1) connects this concept to a DIFFERENT domain; (2) presents a specific claim to evaluate — not open-ended; (3) difficulty for 4 years strategy consulting + MBA.",
    "further_reading": [
        {{"title": "Specific real title", "author": "Author or empty string", "reason": "One sentence on the different angle this adds"}},
        {{"title": "...", "author": "...", "reason": "..."}},
        {{"title": "...", "author": "...", "reason": "..."}}
    ],
    "core_concept": "The most precise one-sentence formulation of this concept for spaced repetition."
}}"""

    user_text = f"""Concept: {name}

Reader's explanation: {explanation}

Primary topic: {topic}"""

    try:
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        response = _get_client().messages.create(
            model=model,
            max_tokens=1200,
            system=[{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_text}],
        )
        return _parse_json_response(response.content[0].text)

    except json.JSONDecodeError as e:
        print(f"  [!] Failed to parse Claude response for concept '{name}': {e}")
        return None
    except Exception as e:
        print(f"  [!] Claude API error for concept '{name}': {e}")
        return None
