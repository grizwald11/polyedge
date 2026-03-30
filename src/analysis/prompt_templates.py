"""Structured prompt templates for Claude probability assessments.

Each template is tailored to a market category and includes:
- Role definition as a calibrated probability forecaster
- Market context fields (question, resolution criteria, current price)
- Required output format (structured JSON)
- Calibration instruction
- Base rate anchoring request
"""

from __future__ import annotations

import logging

from src.core.models import MarketCategory

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are a calibrated probability forecaster. Your job is to estimate the true probability of events resolving YES or NO.

CALIBRATION RULES:
- If you estimate 70%, that means in 100 similar situations, approximately 70 should resolve YES.
- You must account for base rates — how often similar events have occurred historically.
- You must consider both sides of the argument before giving your estimate.
- Do not anchor too heavily on the current market price, but do consider it as information.
- Express genuine uncertainty. Avoid false precision.
- Use the provided news context to inform your assessment. If the news contradicts your prior beliefs, update accordingly.
- When structured data is provided (economic indicators, community forecasts, cross-platform prices), treat these as real-time factual inputs. They are current as of today.
- Avoid overconfidence: probabilities above 95% or below 5% are rarely justified unless the outcome is nearly certain (e.g., settled law, already-occurred event). Most "obvious" outcomes still have 5-15% residual uncertainty.
- Avoid underconfidence: if the evidence strongly favors one outcome, do not hedge to 50-60% out of false modesty.
- Your confidence interval should reflect genuine uncertainty, not a formulaic spread around the point estimate.

DECOMPOSITION METHOD:
When the question involves compound events (A AND B, sequential steps, conditional outcomes), decompose it:
1. Break the question into independent sub-questions with individual probabilities.
2. For AND (all must happen): multiply the sub-probabilities.
3. For OR (at least one): use 1 - product of (1 - each sub-probability).
4. For conditional: P(A and B) = P(A) × P(B|A).
5. State the decomposition in your reasoning.

CRITICAL: Respond ONLY with a valid JSON object. No explanation, no markdown, no code fences, no text before or after the JSON. Your entire response must be parseable as JSON.

Required JSON schema:
{"probability": <float 0.01-0.99>, "confidence_low": <float>, "confidence_high": <float>, "key_factors_for": ["<string>", ...], "key_factors_against": ["<string>", ...], "uncertainties": ["<string>", ...], "reasoning": "<brief explanation including decomposition if applicable>"}"""


POLITICS_TEMPLATE = """Assess the probability of this POLITICAL market resolving YES.

MARKET: {question}
RESOLUTION CRITERIA: {resolution_criteria}
CURRENT MARKET PRICE: {market_price:.0%} (YES)
MARKET CLOSES: {close_date}

CONTEXT:
{news_context}
{base_rate_context}

Consider:
1. Historical base rates for similar political events
2. Current polling data and trends
3. Institutional dynamics and key decision-makers
4. Timeline constraints
5. Whether the resolution criteria has a specific technical definition that differs from common understanding

Provide your probability estimate as JSON."""


FED_MACRO_TEMPLATE = """Assess the probability of this ECONOMIC/FED market resolving YES.

MARKET: {question}
RESOLUTION CRITERIA: {resolution_criteria}
CURRENT MARKET PRICE: {market_price:.0%} (YES)
MARKET CLOSES: {close_date}

CONTEXT:
{news_context}
{base_rate_context}

Consider:
1. Historical base rates for similar Fed actions / economic indicators
2. Current economic data (CPI, PCE, unemployment, GDP)
3. Fed meeting schedule and recent FOMC communications
4. Market expectations (Fed Funds futures, CME FedWatch)
5. Macro environment and global factors

Provide your probability estimate as JSON."""


GEOPOLITICS_TEMPLATE = """Assess the probability of this GEOPOLITICAL market resolving YES.

MARKET: {question}
RESOLUTION CRITERIA: {resolution_criteria}
CURRENT MARKET PRICE: {market_price:.0%} (YES)
MARKET CLOSES: {close_date}

CONTEXT:
{news_context}
{base_rate_context}

Consider:
1. Historical base rates for similar geopolitical events
2. Current diplomatic and military developments
3. Key actors' stated positions and likely incentives
4. Intelligence assessments and expert analysis
5. Timeline and escalation dynamics

Provide your probability estimate as JSON."""


TECH_AI_TEMPLATE = """Assess the probability of this TECH/AI market resolving YES.

MARKET: {question}
RESOLUTION CRITERIA: {resolution_criteria}
CURRENT MARKET PRICE: {market_price:.0%} (YES)
MARKET CLOSES: {close_date}

CONTEXT:
{news_context}
{base_rate_context}

Consider:
1. Historical base rates for similar tech events (product launches, benchmarks, etc.)
2. Company track record and recent announcements
3. Technical feasibility and development timelines
4. Regulatory environment
5. Competitive dynamics

Provide your probability estimate as JSON."""


CULTURE_TEMPLATE = """Assess the probability of this CULTURE/ENTERTAINMENT market resolving YES.

MARKET: {question}
RESOLUTION CRITERIA: {resolution_criteria}
CURRENT MARKET PRICE: {market_price:.0%} (YES)
MARKET CLOSES: {close_date}

CONTEXT:
{news_context}
{base_rate_context}

Consider:
1. Historical base rates and precedents
2. Expert predictions and betting odds
3. Recent public sentiment and trends
4. Key decision-makers or voting bodies

Provide your probability estimate as JSON."""


GENERAL_TEMPLATE = """Assess the probability of this market resolving YES.

MARKET: {question}
RESOLUTION CRITERIA: {resolution_criteria}
CURRENT MARKET PRICE: {market_price:.0%} (YES)
MARKET CLOSES: {close_date}

CONTEXT:
{news_context}
{base_rate_context}

Consider:
1. Historical base rates for similar events
2. Current relevant data and developments
3. Key factors that could push the outcome either way
4. Timeline constraints
5. Whether the resolution criteria has a specific technical definition

Provide your probability estimate as JSON."""


NEWS_IMPACT_TEMPLATE = """BREAKING NEWS IMPACT ASSESSMENT

A breaking news article may affect the following prediction market.

BREAKING NEWS:
Headline: {headline}
Summary: {summary}
Source: {source}

MARKET: {question}
RESOLUTION CRITERIA: {resolution_criteria}
CURRENT MARKET PRICE: {market_price:.0%} (YES)

Assess how this news changes the probability of the market resolving YES.
Consider:
1. How directly does this news relate to the market's resolution criteria?
2. How much should the probability shift based on this information?
3. Has the market likely already priced in this information?
4. What is your updated probability estimate?

Provide your probability estimate as JSON."""


ARB_VALIDATION_TEMPLATE = """ARBITRAGE RELATIONSHIP VALIDATION

Determine if these two prediction markets have a logical relationship.

MARKET A: {question_a}
CURRENT PRICE A: {price_a:.0%} (YES)

MARKET B: {question_b}
CURRENT PRICE B: {price_b:.0%} (YES)

Questions:
1. If Market A resolves YES, must Market B also resolve YES? (A is subset of B)
2. If Market B resolves YES, must Market A also resolve YES? (B is subset of A)
3. Can both markets resolve YES simultaneously, or are they mutually exclusive?
4. Is the current pricing logically consistent?

Respond with JSON: {{"relationship": "subset_ab"|"subset_ba"|"mutual_exclusive"|"independent"|"correlated", "confidence": <float 0-1>, "reasoning": "<explanation>", "arbitrage_exists": <bool>, "suggested_direction": "<description or null>"}}"""


# Map categories to templates
CATEGORY_TEMPLATES: dict[MarketCategory, str] = {
    MarketCategory.POLITICS: POLITICS_TEMPLATE,
    MarketCategory.FED_MACRO: FED_MACRO_TEMPLATE,
    MarketCategory.GEOPOLITICS: GEOPOLITICS_TEMPLATE,
    MarketCategory.TECH_AI: TECH_AI_TEMPLATE,
    MarketCategory.CULTURE: CULTURE_TEMPLATE,
    MarketCategory.EARNINGS: GENERAL_TEMPLATE,
    MarketCategory.CRYPTO: GENERAL_TEMPLATE,
    MarketCategory.SPORTS: GENERAL_TEMPLATE,
    MarketCategory.OTHER: GENERAL_TEMPLATE,
}


def get_template(category: MarketCategory) -> str:
    """Get the prompt template for a market category."""
    return CATEGORY_TEMPLATES.get(category, GENERAL_TEMPLATE)


def _sanitize_external_text(text: str, max_length: int = 5000) -> str:
    """Sanitize text from external sources (market descriptions, news) before prompt injection.

    Uses a three-layer defense:
    1. Truncate and strip control characters / zero-width unicode.
    2. Detect and REMOVE entire sentences containing known injection patterns.
    3. Character allowlist: strip any character outside the safe set (printable
       ASCII + common accented letters + basic punctuation). This catches novel
       injection techniques that bypass pattern matching.
    """
    import re
    # Truncate to prevent oversized injections
    original_len = len(text)
    text = text[:max_length]
    if original_len > max_length:
        logger.debug(f"Sanitize: truncated text from {original_len} to {max_length} chars")

    # Layer 1: Strip control characters and zero-width unicode
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', text)
    text = re.sub(r'[\u200b-\u200f\u2028-\u202f\u2060\ufeff]', '', text)

    # Layer 2: Detect and REMOVE entire sentences containing injection patterns.
    # Removing just the keyword leaves surrounding manipulative context intact.
    injection_patterns = [
        (r"ignore\s+(all\s+)?previous\s+instructions", "ignore previous instructions"),
        (r"you\s+are\s+now\s+", "role override attempt"),
        (r"system\s*:\s*", "system: prefix"),
        (r"assistant\s*:\s*", "assistant: prefix"),
        (r"human\s*:\s*", "human: prefix"),
        (r"<\s*/?system\s*>", "system tag"),
        (r"output\s+probability\s+\d", "probability override"),
        (r"forget\s+(all\s+)?(your|prior)", "memory wipe attempt"),
        (r"new\s+instructions?\s*:", "instruction override"),
    ]
    for pattern, description in injection_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            logger.warning(
                f"Prompt injection pattern STRIPPED ({description}) "
                f"from external text: {text[:100]!r}..."
            )
            # Remove the entire sentence containing the injection
            sentence_pattern = r'[^.!?\n]*' + pattern + r'[^.!?\n]*[.!?\n]?'
            text = re.sub(sentence_pattern, '', text, flags=re.IGNORECASE)

    # Layer 3: Character allowlist — only permit safe characters.
    # Allows: printable ASCII (space through ~), common accented/international
    # letters (Latin-1 Supplement, Latin Extended-A), basic punctuation, and
    # standard whitespace (newline, tab). Everything else is stripped.
    text = re.sub(r'[^\x20-\x7E\u00C0-\u024F\n\t]', '', text)

    return text.strip()


def build_prompt(
    question: str,
    resolution_criteria: str,
    market_price: float,
    close_date: str,
    category: MarketCategory,
    news_context: str = "No additional context available.",
    base_rate_context: str = "",
) -> str:  # M-6: explicit return type
    """Build a complete prompt for Claude from market data.

    All external text (question, resolution criteria, news) is sanitized
    to mitigate prompt injection from untrusted API sources.
    """
    template = get_template(category)
    return template.format(
        question=_sanitize_external_text(question, 500),
        resolution_criteria=_sanitize_external_text(
            resolution_criteria or "Standard market resolution rules apply.", 2000
        ),
        market_price=market_price,
        close_date=close_date or "Not specified",
        news_context=_sanitize_external_text(
            news_context or "No additional context available.", 5000
        ),
        base_rate_context=_sanitize_external_text(base_rate_context, 2000),
    )
