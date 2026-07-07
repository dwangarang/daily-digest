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

# Named frameworks used as cross-domain lenses in think_about_this questions.
# Claude picks the most apt one per article; the curator enforces max-2-same per digest.
FRAMEWORKS = [
    ("Musk — First Principles",      "strip analogy/convention; identify fundamental constraints, reason up from physics"),
    ("Bezos — Regret Minimization",  "project to age 80: which choice would you regret not making? prioritize irreversibility"),
    ("Schelling — Focal Points",     "coordination without communication: where do agents with no instructions naturally converge?"),
    ("Munger — Inversion",           "ask what guarantees failure, then avoid it; think backwards to surface hidden risks"),
    ("Christensen — Disruption",     "low-end or new-market entrants improve incrementally until they displace incumbents"),
    ("Taleb — Antifragility",        "distinguish fragile/robust/antifragile; seek systems that gain from disorder"),
    ("Coase — Theory of the Firm",   "firms exist to reduce transaction costs; boundaries set where markets are inefficient"),
    ("Boyd — OODA Loop",             "Observe-Orient-Decide-Act; faster loops beat stronger opponents; orientation is decisive"),
    ("Kahneman — Prospect Theory",   "losses loom twice as large as gains; reference points determine risk appetite"),
    ("Porter — Five Forces",         "industry profitability shaped by supplier power, buyer power, substitutes, entrants, rivalry"),
    ("Thiel — Zero to One",          "monopolies create and capture value; competition destroys it; seek secrets others deny"),
    ("Marks — Second-Level Thinking","first level: obvious; second level: what does consensus think, and where is it wrong?"),
    ("Dalio — Debt Cycles",          "short- and long-term debt cycles drive macro outcomes most actors underweight"),
    ("Perez — Tech Revolutions",     "each wave: installation surge → bubble → crash → deployment synergy → maturity"),
    ("Keynes — Beauty Contest",      "markets price what others think others think; second/third-order beliefs dominate short run"),
]

# Rhetorical opening moves for the insight field. One is assigned per article
# (deterministically, by article id) so consecutive items don't share the same
# opening structure and no single formula becomes the house style.
OPENING_MOVES = [
    "MECHANISM-FIRST: open by naming the causal mechanism at work, then what it produces",
    "CONSEQUENCE-FIRST: open with the concrete downstream effect, then trace it back",
    "TENSION-FIRST: open with the specific tension or tradeoff the piece exposes",
    "NUMBER-FIRST: open with the load-bearing number or concrete fact, then what it implies",
    "ACTOR-FIRST: open with who gains or loses and why, then generalize",
]


def assigned_opening_move(article_id: str) -> str:
    """Deterministically rotate opening structures so items vary."""
    try:
        idx = int(str(article_id)[:8], 16) % len(OPENING_MOVES)
    except ValueError:
        idx = sum(ord(c) for c in str(article_id)) % len(OPENING_MOVES)
    return OPENING_MOVES[idx]


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
    framework_list = "\n".join(f"- {name}: {desc}" for name, desc in FRAMEWORKS)
    return f"""You are a sharp research analyst processing articles for a specific reader. Your job is not to summarize — it is to extract what's non-obvious and frame it for someone with 4 years of strategy consulting experience and an MBA focused on AI, enterprise SaaS GTM, and China-ASEAN markets.

Reader profile:
{interest_profile}

Available topic tags: {json.dumps(topic_names)}

FRAMEWORK LENSES (for cross-domain synthesis):
{framework_list}

Return a JSON object with this exact structure (no markdown fences, no other text):
{{
    "context": "1-2 neutral sentences orienting the reader: what is this piece, who wrote/published it, and what is its central claim or event? No opinion here — just enough to know what you're about to evaluate.",
    "insight": "The single non-obvious or contrarian takeaway. What would a smart person miss on first read? What's the second-order implication? Direct, opinionated voice. 2-3 sentences max.",
    "so_what": "One sentence: the piece's most natural implication. See rules — do NOT force it through the reader's professional domains.",
    "contrarian_angle": "One sentence: the strongest counterargument to the article's thesis, or the thing most readers would get wrong about it. Empty string if none is genuinely strong — see rules.",
    "key_takeaways": ["takeaway 1", "takeaway 2", "takeaway 3"],
    "tags": ["tag1"],
    "relevance_score": 0.75,
    "think_about_this": "Cross-domain synthesis question using the framework you chose. Apply that framework's specific logic to the article's concept. Make a concrete claim the reader evaluates — not 'how would you...' but 'claim X implies Y — do you agree?'. Difficulty: 4yr strategy consulting + MBA.",
    "think_framework": "Exact name of the framework you used, copied from the FRAMEWORK LENSES list — e.g. 'Boyd — OODA Loop'",
    "further_reading": [
        {{"title": "Specific title", "author": "Author name or empty string", "format": "research paper", "reason": "One sentence on the distinct angle this adds"}},
        {{"title": "Specific title", "author": "Author or empty string", "format": "memo", "reason": "One sentence on the distinct angle"}},
        {{"title": "Specific title", "author": "Author or empty string", "format": "essay", "reason": "One sentence on the distinct angle"}}
    ],
    "core_concept": "The single most important idea in one sentence. Standalone insight for spaced repetition — not a reference to the article.",
    "takeaway": "One concrete action or mental update the reader should make. Specific enough to apply immediately — not 'think about AI risk' but 'when evaluating AI infrastructure vendors, treat switching costs as the primary filter before price.'",
    "historical_analog": {{
        "event": "Specific historical event, period, or case — e.g. '2008 CDO market collapse', 'Japan 1990s asset bubble'",
        "mechanism": "The structural parallel — WHY it rhymes mechanically, not just what it superficially resembles",
        "key_difference": "The most important way this situation structurally differs from the historical case",
        "counter_case": "A historical case where similar conditions did NOT produce the feared outcome, and the key reason why",
        "confidence": "high | medium | speculative"
    }}
}}

Rules:
- context: purely neutral orientation. No opinion. If the reader doesn't know this source/author, this sentence tells them.
- insight: opinionated, not neutral. Second-order thinking, not first read.
- insight STRUCTURE: use the OPENING MOVE named in the user message. BANNED as an opener (and as a crutch anywhere): the inversion template "the real X isn't Y, it's Z" / "this isn't about X, it's about Y" in any phrasing. If you catch yourself writing it, restructure the sentence around the assigned opening move instead.
- so_what: find the piece's OWN most natural implication first — the consequence a thoughtful generalist would draw. Connect it to the reader's domains (AI strategy, SaaS GTM, China-ASEAN) ONLY when that connection is genuine and specific. A 2008 credit memo's natural implication is about risk and cycles, not SaaS GTM — forcing every piece through one professional frame flattens what's distinct about it. Never stretch.
- contrarian_angle: steelman the opposing view or surface the common misreading — but ONLY if a genuinely strong one exists. Return "" rather than a manufactured counterpoint. An absent section signals more than a weak one.
- tags: pick 1-2 from the available topic tags only
- relevance_score: 0.0-1.0 based on match with reader profile
- think_about_this: MUST use one framework from the FRAMEWORK LENSES list as the cross-domain lens. Pick the most apt one — don't default to Marks for everything.
- think_framework: copy the exact name string from the list — this is used to enforce digest-level framework diversity
- further_reading: include exactly 3 recommendations — one academic/research piece, one practitioner piece (memo, blog post, interview, or talk), one long-form essay or book chapter. No two from the same author. Be specific: "Howard Marks' October 2001 memo 'You Can't Predict. You Can Prepare.'" not "a Howard Marks memo". Format values: memo | essay | research paper | talk | interview | blog post | book chapter
- core_concept: a standalone insight, not a reference to the article
- takeaway: one concrete thing to do or believe differently. Not a summary. Actionable.
- historical_analog: ONLY return non-null when there is a genuine structural mechanism parallel at HIGH confidence — the mechanism must map, not the surface. Return null otherwise; most articles should have null. The key_difference must be stated honestly even when the analog is strong."""


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
            f"SOURCE: {article['source_name']}\n"
            f"OPENING MOVE for the insight field: {assigned_opening_move(article['id'])}\n\n"
            f"CONTENT:\n{content[:8000]}"
        )
        requests.append({
            "custom_id": article["id"],
            "params": {
                "model": model,
                "max_tokens": 2000,
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
        f"SOURCE: {article['source_name']}\n"
        f"OPENING MOVE for the insight field: {assigned_opening_move(article.get('id', article['title']))}\n\n"
        f"CONTENT:\n{content[:8000]}"
    )

    try:
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        response = _get_client().messages.create(
            model=model,
            max_tokens=2000,
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

    framework_list = "\n".join(f"- {name}: {desc}" for name, desc in FRAMEWORKS)
    system_text = f"""You are a sharp research analyst helping a reader deepen their understanding of a concept they already know. Add the non-obvious angle and connect it to their professional context.

Reader profile:
{interest_profile}

Available topic tags: {json.dumps(topic_names)}

FRAMEWORK LENSES (for cross-domain synthesis):
{framework_list}

The reader will provide a concept name and their own explanation. Return a JSON object with this exact structure (no markdown fences, no other text):
{{
    "insight": "The non-obvious angle on this concept. What would a second-order thinker add? What implication or edge case does it point to? 2-3 sentences, direct and opinionated.",
    "so_what": "One sentence connecting this concept to the reader's professional context — AI strategy, SaaS GTM, or China-ASEAN markets.",
    "contrarian_angle": "One sentence: the strongest objection, or the most common way practitioners misapply it.",
    "key_takeaways": ["takeaway 1", "takeaway 2", "takeaway 3"],
    "tags": ["tag from available list"],
    "relevance_score": 0.85,
    "think_about_this": "Cross-domain synthesis question using one framework from the FRAMEWORK LENSES list. Apply that framework's logic to the concept. Make a specific claim to evaluate, not open-ended. Difficulty: 4yr consulting + MBA.",
    "think_framework": "Exact name copied from FRAMEWORK LENSES list",
    "further_reading": [
        {{"title": "Specific real title", "author": "Author or empty string", "format": "research paper", "reason": "One sentence on the distinct angle"}},
        {{"title": "Specific real title", "author": "Author or empty string", "format": "memo", "reason": "One sentence"}},
        {{"title": "Specific real title", "author": "Author or empty string", "format": "essay", "reason": "One sentence"}}
    ],
    "core_concept": "The most precise one-sentence formulation of this concept for spaced repetition."
}}

Rules for further_reading: one academic/research piece, one practitioner piece (memo/blog/interview/talk), one essay or book chapter. No two from the same author. Be specific about titles."""

    user_text = f"Concept: {name}\n\nReader's explanation: {explanation}\n\nPrimary topic: {topic}"

    try:
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        response = _get_client().messages.create(
            model=model,
            max_tokens=2000,
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
