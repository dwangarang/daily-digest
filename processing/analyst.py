"""
Expert lens analysis and Opus export prompt generation.

generate_expert_analyses(): applies 2-3 named expert frameworks to a single article.
build_opus_prompt(): builds a structured deep-dive prompt for the user to paste into
    claude.ai (Opus) for interactive exploration — uses Pro subscription, zero API cost.
"""

import os
import json
from anthropic import Anthropic

client = None


def _get_client():
    global client
    if client is None:
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return client


def _select_experts(article: dict, experts: list) -> list:
    """Pick the 2-3 most domain-relevant experts for this article."""
    tags = article.get("tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            tags = []

    scored = []
    for expert in experts:
        overlap = sum(1 for tag in tags if tag in expert.get("domains", []))
        scored.append((overlap, expert))

    scored.sort(key=lambda x: x[0], reverse=True)
    selected = [e for score, e in scored if score > 0][:3]

    if not selected:
        selected = [e for _, e in scored[:2]]

    return selected


def generate_expert_analyses(article: dict, config: dict) -> list:
    """
    For a single article, generate analysis through 2-3 relevant expert lenses.
    Called at digest-send time (synchronous). Returns list of {expert, analysis}.
    """
    experts = config.get("experts", [])
    if not experts:
        return []

    selected = _select_experts(article, experts)
    if not selected:
        return []

    expert_list = "\n".join(
        f"- {e['name']}: {e['framework']}" for e in selected
    )
    insight = article.get("insight") or article.get("summary", "")

    prompt = f"""Apply each expert's documented framework to this article's core insight. Reference their specific documented concepts by name — not generic wisdom dressed in their name.

ARTICLE: {article.get('title', '')}
SOURCE: {article.get('source_name', '')}
CORE INSIGHT: {insight}
CONTRARIAN ANGLE: {article.get('contrarian_angle', '')}

EXPERT FRAMEWORKS:
{expert_list}

Return a JSON array (no markdown, no other text):
[
  {{
    "expert": "Expert Name",
    "analysis": "2-3 sentences. Apply their specific documented framework — name their actual concepts. If this article genuinely falls outside their framework's domain, say that in one sentence rather than forcing a connection."
  }}
]"""

    try:
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        response = _get_client().messages.create(
            model=model,
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        return json.loads(raw.strip())
    except Exception as e:
        print(f"  [!] Expert analysis failed for '{article.get('title', '')[:50]}': {e}")
        return []


def generate_lens_framing(lens_article: dict, digest_articles: list, config: dict) -> str:
    """
    Apply a lens-role source's essay (e.g. Paul Graham) as an interpretive framework
    across today's digest, rather than treating it as a competing content item.
    Called once per digest, at most. Returns "" on failure.
    """
    insight = lens_article.get("insight") or lens_article.get("summary", "")
    titles = "\n".join(f"- {a.get('title', '')}: {a.get('insight') or a.get('summary', '')}"
                        for a in digest_articles)

    prompt = f"""An essay offers a mental model. Apply that model to connect or reframe today's digest items — don't summarize the essay itself.

ESSAY: {lens_article.get('title', '')}
SOURCE: {lens_article.get('source_name', '')}
CORE IDEA: {insight}

TODAY'S DIGEST ITEMS:
{titles}

Write 2-3 sentences applying the essay's specific idea to what connects (or productively conflicts with) today's items. Name the essay's actual concept, not generic wisdom. Return plain text only, no markdown, no preamble."""

    try:
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        response = _get_client().messages.create(
            model=model,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        print(f"  [!] Lens framing failed for '{lens_article.get('title', '')[:50]}': {e}")
        return ""


def build_opus_prompt(article: dict, expert_analyses: list, config: dict) -> str:
    """
    Build a structured deep-dive prompt for the user to paste into claude.ai (Opus).
    Uses Pro subscription — zero marginal API cost.
    """
    interest_profile = config.get("interest_profile", "")

    expert_section = ""
    if expert_analyses:
        expert_section = "\n\nEXPERT LENS ANALYSES (Sonnet-generated — challenge and extend these):\n"
        for ea in expert_analyses:
            expert_section += f"\n{ea.get('expert', '')}:\n{ea.get('analysis', '')}\n"

    historical = article.get("historical_analog")
    if isinstance(historical, str):
        try:
            historical = json.loads(historical)
        except Exception:
            historical = None

    historical_section = ""
    if historical and historical.get("event"):
        historical_section = f"""
HISTORICAL ANALOG ({historical.get('confidence', 'medium')} confidence):
Event: {historical.get('event', '')}
Mechanism: {historical.get('mechanism', '')}
Key difference: {historical.get('key_difference', '')}
Counter-case: {historical.get('counter_case', '')}"""

    return f"""You are assisting with deep analysis of a specific article. The reader has 4 years of strategy consulting experience and an MBA focused on AI, enterprise SaaS GTM, and China-ASEAN markets.

ARTICLE: {article.get('title', '')}
SOURCE: {article.get('source_name', '')}
URL: {article.get('url', '')}

SURFACE ANALYSIS (Sonnet-generated — your job is to challenge and extend this):
Insight: {article.get('insight') or article.get('summary', '')}
So what: {article.get('so_what', '')}
Contrarian angle: {article.get('contrarian_angle', '')}
Think about this: {article.get('think_about_this', '')}{expert_section}{historical_section}

READER PROFILE:
{interest_profile}

YOUR TASK:
Identify the single most underexplored angle in the surface analysis above. Then choose whichever of these directions adds the most net-new value:
1. Apply a framework the surface analysis missed — name it and apply it rigorously
2. Steelman the contrarian angle — make it stronger than stated
3. Identify second and third-order implications for the reader's specific domains (AI GTM, China-ASEAN, SaaS strategy)
4. Stress-test the article's central claim — what would have to be true for it to be wrong?

Do not summarize what's already written. Every sentence should add analytical content not present above."""
