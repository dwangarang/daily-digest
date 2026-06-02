"""
Summarization and analysis via Claude API.

process_articles_batch() uses the Batch API (50% cost vs synchronous).
process_article() is the synchronous fallback used by add-concept mode.
process_concept() handles manually entered concepts.
"""

import os
import json
import time
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


def _build_article_system_prompt(interest_profile: str, topic_names: list) -> str:
    return f"""You are a sharp research analyst processing articles for a specific reader. Your job is not to summarize — it is to extract what's non-obvious and frame it for someone with 4 years of strategy consulting experience and an MBA focused on AI, enterprise SaaS GTM, and China-ASEAN markets.

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
- relevance_score: 0.0-1.0 based on match with reader profile
- think_about_this: must be cross-domain, must make a specific claim, must not be yes/no answerable
- further_reading: real titles with specificity
- core_concept: a standalone insight, not a reference to the article"""


def _fetch_content(article: dict) -> str:
    """Return article content, fetching from URL if raw_content is absent."""
    content = article.get("raw_content", "")
    if not content or content.startswith("[External link:"):
        url = article.get("url", "")
        if url:
            content = fetch_full_article(url)
    return content


def process_articles_batch(articles: list, config: dict) -> dict:
    """
    Process a list of articles using the Batch API (50% cost reduction).

    Fetches content for each article, submits one batch request to Anthropic,
    polls until complete, then returns {article_id: result_dict or None}.
    Falls back to synchronous processing if the batch API fails.
    """
    if not articles:
        return {}

    interest_profile = config.get("interest_profile", "")
    topics = config.get("topics", [])
    topic_names = [t["name"] for t in topics]
    system_text = _build_article_system_prompt(interest_profile, topic_names)
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    requests = []
    no_content = {}

    for article in articles:
        content = _fetch_content(article)
        if not content:
            print(f"    [!] No content for: {article['title'][:60]}")
            no_content[article["id"]] = None
            continue

        user_text = (
            f"Analyze this article:\n\n"
            f"TITLE: {article['title']}\n"
            f"SOURCE: {article['source_name']}\n\n"
            f"CONTENT:\n{content[:8000]}"
        )
        requests.append({
            "custom_id": article["id"],
            "params": {
                "model": model,
                "max_tokens": 1200,
                "system": [{"type": "text", "text": system_text,
                             "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": user_text}],
            },
        })

    if not requests:
        return no_content

    try:
        c = _get_client()
        batch = c.messages.batches.create(requests=requests)
        print(f"    Batch submitted ({len(requests)} articles, id: {batch.id})")

        # Poll until done, max 30 minutes
        deadline = time.time() + 1800
        while batch.processing_status == "in_progress":
            if time.time() > deadline:
                print("    [!] Batch timed out — falling back to synchronous.")
                return _sync_fallback(articles, config)
            time.sleep(30)
            batch = c.messages.batches.retrieve(batch.id)
            remaining = batch.request_counts.processing
            print(f"    Batch: {remaining} remaining...")

        results = dict(no_content)
        for item in c.messages.batches.results(batch.id):
            if item.result.type == "succeeded":
                try:
                    results[item.custom_id] = _parse_json_response(
                        item.result.message.content[0].text
                    )
                except Exception as e:
                    print(f"    [!] Parse error for {item.custom_id}: {e}")
                    results[item.custom_id] = None
            else:
                print(f"    [!] {item.result.type}: {item.custom_id}")
                results[item.custom_id] = None

        succeeded = sum(1 for v in results.values() if v is not None)
        print(f"    Batch complete: {succeeded}/{len(articles)} succeeded")
        return results

    except Exception as e:
        print(f"    [!] Batch API error ({e}) — falling back to synchronous.")
        return _sync_fallback(articles, config)


def _sync_fallback(articles: list, config: dict) -> dict:
    """Synchronous per-article processing, used when batch API is unavailable."""
    return {a["id"]: process_article(a, config) for a in articles}


def process_article(article: dict, config: dict) -> dict | None:
    """
    Process a single article synchronously. Used by add-concept mode
    and as the sync fallback inside process_articles_batch().
    """
    content = _fetch_content(article)
    if not content:
        print(f"  [!] No content available for: {article['title']}")
        return None

    interest_profile = config.get("interest_profile", "")
    topics = config.get("topics", [])
    topic_names = [t["name"] for t in topics]
    system_text = _build_article_system_prompt(interest_profile, topic_names)

    user_text = (
        f"Analyze this article:\n\n"
        f"TITLE: {article['title']}\n"
        f"SOURCE: {article['source_name']}\n\n"
        f"CONTENT:\n{content[:8000]}"
    )

    try:
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        response = _get_client().messages.create(
            model=model,
            max_tokens=1200,
            system=[{"type": "text", "text": system_text,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_text}],
        )
        return _parse_json_response(response.content[0].text)

    except json.JSONDecodeError as e:
        print(f"  [!] Parse error for '{article['title']}': {e}")
        return None
    except Exception as e:
        print(f"  [!] API error for '{article['title']}': {e}")
        return None


def process_concept(name: str, explanation: str, topic: str, config: dict) -> dict | None:
    """
    Process a manually entered concept through Claude (always synchronous).
    """
    interest_profile = config.get("interest_profile", "")
    topics = config.get("topics", [])
    topic_names = [t["name"] for t in topics]

    system_text = f"""You are a sharp research analyst helping a reader deepen their understanding of a concept they already know. Add the non-obvious angle and connect it to their professional context.

Reader profile:
{interest_profile}

Available topic tags: {json.dumps(topic_names)}

The reader will provide a concept name and their own explanation. Return a JSON object with this exact structure (no markdown fences, no other text):
{{
    "insight": "The non-obvious angle on this concept. What would a second-order thinker add? What implication or edge case does it point to? 2-3 sentences, direct and opinionated.",
    "so_what": "One sentence connecting this concept to the reader's professional context — AI strategy, SaaS GTM, or China-ASEAN markets.",
    "contrarian_angle": "One sentence: the strongest objection, or the most common way practitioners misapply it.",
    "key_takeaways": ["takeaway 1", "takeaway 2", "takeaway 3"],
    "tags": ["tag from available list"],
    "relevance_score": 0.85,
    "think_about_this": "Cross-domain synthesis question: (1) connects to a DIFFERENT domain; (2) specific claim to evaluate, not open-ended; (3) difficulty for 4yr strategy consulting + MBA.",
    "further_reading": [
        {{"title": "Specific real title", "author": "Author or empty string", "reason": "One sentence on the different angle this adds"}},
        {{"title": "...", "author": "...", "reason": "..."}},
        {{"title": "...", "author": "...", "reason": "..."}}
    ],
    "core_concept": "The most precise one-sentence formulation of this concept for spaced repetition."
}}"""

    user_text = f"Concept: {name}\n\nReader's explanation: {explanation}\n\nPrimary topic: {topic}"

    try:
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        response = _get_client().messages.create(
            model=model,
            max_tokens=1200,
            system=[{"type": "text", "text": system_text,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_text}],
        )
        return _parse_json_response(response.content[0].text)

    except json.JSONDecodeError as e:
        print(f"  [!] Parse error for concept '{name}': {e}")
        return None
    except Exception as e:
        print(f"  [!] API error for concept '{name}': {e}")
        return None
