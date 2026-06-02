"""
Summarization and relevance scoring via Claude API.

Uses prompt caching: the interest profile and topic list are sent as a
cached system prompt, so repeated calls within a run only pay for the
article content itself.
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


def process_article(article: dict, config: dict) -> dict | None:
    """
    Process a single article through Claude.

    Returns a dict with: summary, key_takeaways, tags, relevance_score,
    think_about_this, core_concept, related_search_terms.
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

    system_text = f"""You are a research assistant analyzing articles for a reader with this profile:

{interest_profile}

Available topic tags: {json.dumps(topic_names)}

Analyze the article the user provides and return a JSON object with this exact structure (no markdown fences, no other text):
{{
    "summary": "2-3 sentence summary. Include the high-level takeaway AND one concrete example or data point.",
    "key_takeaways": ["takeaway 1", "takeaway 2", "takeaway 3"],
    "tags": ["tag1", "tag2"],
    "relevance_score": 0.75,
    "think_about_this": "One thought-provoking question that connects this article to the reader's interests",
    "related_search_terms": ["term1", "term2", "term3"],
    "core_concept": "The single most important idea from this article, stated in one sentence"
}}

Rules:
- tags: pick 1-2 from the available topic tags only
- relevance_score: 0.0 to 1.0 based on how well this matches the reader profile
- think_about_this: should require APPLICATION of the idea, not just recall
- core_concept: this will be used for spaced repetition — make it a standalone insight
- related_search_terms: 3 terms someone could search to find different perspectives on this topic"""

    user_text = f"""Analyze this article:

TITLE: {article['title']}
SOURCE: {article['source_name']}

CONTENT:
{content[:8000]}"""

    try:
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        response = _get_client().messages.create(
            model=model,
            max_tokens=1000,
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

        return json.loads(raw_text)

    except json.JSONDecodeError as e:
        print(f"  [!] Failed to parse Claude response for '{article['title']}': {e}")
        return None
    except Exception as e:
        print(f"  [!] Claude API error for '{article['title']}': {e}")
        return None
