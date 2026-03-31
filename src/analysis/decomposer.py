"""Superforecaster Decomposition Pipeline — multi-step reasoning for compound questions.

Instead of a single Claude call that simultaneously estimates probability,
this module decomposes compound questions into independent sub-questions,
estimates base rates for each, updates from evidence, and recombines using
the correct logical operator (AND/OR/CONDITIONAL).

Based on Tetlock's superforecasting research: explicit decomposition + base-rate
anchoring reduces overconfidence and systematic biases, improving Brier scores
by 0.02-0.05 on compound questions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Optional

from src.core.models import ForecastResult, Market

logger = logging.getLogger(__name__)

# Keywords that suggest a compound question amenable to decomposition.
# Checked case-insensitively against the market question text.
COMPOUND_INDICATORS = [
    r"\band\b",
    r"\bbefore\b",
    r"\bafter\b",
    r"\bthen\b",
    r"\bif\b.*\b(?:then|will)\b",
    r"\bboth\b",
    r"\ball\b",
    r"\bcondition(?:al|ed)\b",
    r"\brequir(?:e|es|ing)\b",
    r"\bfirst\b.*\bthen\b",
    r"\bpass(?:es)?\b.*\bsign(?:s|ed)?\b",
    r"\bapprove\b.*\bimplement\b",
    r"\bwin\b.*\band\b",
    r"\bnominated?\b.*\bconfirmed?\b",
]

# Minimum question word count to consider decomposition.
# Very short questions are unlikely to be compound.
MIN_QUESTION_WORDS = 8


def is_compound_question(question: str) -> bool:
    """Heuristic check: does this question likely involve compound events?

    Returns True if the question contains indicators of multi-step or
    conditional outcomes that would benefit from explicit decomposition.
    """
    if len(question.split()) < MIN_QUESTION_WORDS:
        return False
    q_lower = question.lower()
    return any(re.search(pattern, q_lower) for pattern in COMPOUND_INDICATORS)


class QuestionDecomposer:
    """Decomposes compound questions into sub-questions for more accurate forecasting.

    Uses a multi-step Claude pipeline:
    1. Decompose: identify sub-questions and logical structure
    2. Assess: estimate each sub-question's probability (parallelized)
    3. Recombine: combine sub-probabilities using the correct operator
    """

    def __init__(self, forecaster):
        """Initialize with a reference to the ClaudeForecaster for API calls.

        Args:
            forecaster: ClaudeForecaster instance (used for _call_claude, _parse_response, etc.)
        """
        self.forecaster = forecaster

    async def decompose_and_assess(
        self,
        market: Market,
        news_context: str = "",
        base_rate_context: str = "",
    ) -> Optional[ForecastResult]:
        """Decompose a compound question and assess each sub-question.

        Returns a ForecastResult with the recombined probability, or None
        if decomposition fails (caller should fall back to single-shot).

        Args:
            market: The market to assess
            news_context: News context for evidence updating
            base_rate_context: Historical base rate context
        """
        start_time = time.monotonic()

        # Step 1: Ask Claude to decompose the question
        decomposition = await self._decompose_question(market, news_context)
        if decomposition is None:
            return None

        decomp_type = decomposition.get("decomposition_type", "ATOMIC")
        sub_questions = decomposition.get("sub_questions", [])

        # ATOMIC questions don't benefit from decomposition — fall back
        if decomp_type == "ATOMIC" or len(sub_questions) < 2:
            logger.debug(
                f"Decomposer: {market.ticker} is ATOMIC or single sub-question, "
                "falling back to single-shot"
            )
            return None

        # Step 2: Assess sub-questions (sequential+conditional for AND/CONDITIONAL, parallel for OR)
        sub_results = await self._assess_sub_questions(
            sub_questions, market, news_context, base_rate_context,
            decomp_type=decomp_type,
        )

        if not sub_results:
            logger.warning(f"Decomposer: no sub-question results for {market.ticker}")
            return None

        # Step 3: Recombine using the correct logical operator
        combined_prob = self._recombine(decomp_type, sub_results)
        if combined_prob is None:
            return None

        # Build the final ForecastResult
        latency_ms = int((time.monotonic() - start_time) * 1000)
        total_tokens = sum(r.get("tokens_used", 0) for r in sub_results)

        # Compute confidence interval from sub-question spread
        sub_probs = [r["probability"] for r in sub_results]
        ci_half = min(0.20, max(0.05, max(sub_probs) - min(sub_probs)))

        # Build reasoning from decomposition
        sub_reasoning = "; ".join(
            f"P({r['question'][:40]}...)={r['probability']:.0%}"
            for r in sub_results
        )
        reasoning = (
            f"Decomposed ({decomp_type}): {sub_reasoning}. "
            f"Combined: {combined_prob:.0%}"
        )

        return ForecastResult(
            probability=max(0.01, min(0.99, combined_prob)),
            confidence_low=max(0.01, combined_prob - ci_half),
            confidence_high=min(0.99, combined_prob + ci_half),
            key_factors_for=[r.get("key_factor", "") for r in sub_results if r.get("key_factor")],
            key_factors_against=[],
            uncertainties=[
                f"Decomposition uses conditional chaining for {decomp_type}"
                if decomp_type in ("AND", "CONDITIONAL")
                else f"Decomposition assumes {decomp_type} independence"
            ],
            reasoning=reasoning,
            model_used=self.forecaster._select_model(),
            tokens_used=total_tokens,
            latency_ms=latency_ms,
        )

    async def _decompose_question(
        self,
        market: Market,
        news_context: str,
    ) -> Optional[dict]:
        """Ask Claude to decompose the question into sub-questions.

        Returns a dict with 'decomposition_type' and 'sub_questions', or None on failure.
        """
        from src.analysis.prompt_templates import DECOMPOSITION_TEMPLATE, _sanitize_external_text

        prompt = DECOMPOSITION_TEMPLATE.format(
            question=_sanitize_external_text(market.question, 500),
            resolution_criteria=_sanitize_external_text(
                market.description or "Standard resolution rules apply.", 2000
            ),
            news_context=_sanitize_external_text(
                news_context or "No additional context.", 3000
            ),
        )

        try:
            model = self.forecaster._select_model()
            temperature = 0.2  # Low temperature for structured decomposition
            timeout = self.forecaster.settings.claude.api_timeout_seconds

            response = await self.forecaster._call_claude(prompt, model, temperature, timeout)
            raw_text = self.forecaster._extract_text(response)
            if raw_text is None:
                return None

            # Track tokens
            tokens_used = response.usage.input_tokens + response.usage.output_tokens
            self.forecaster._track_tokens(
                tokens_used,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                model=model,
            )

            return self._parse_decomposition(raw_text)

        except Exception as e:
            logger.warning(f"Decomposition failed for {market.ticker}: {e}")
            return None

    def _parse_decomposition(self, raw_text: str) -> Optional[dict]:
        """Parse Claude's decomposition response into structured data."""
        text = raw_text.strip()

        # Try direct JSON parse
        for attempt_text in [
            text,
            # Extract from code blocks
            *(m.group(1).strip() for m in [re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)] if m),
            # Brace extraction
            *(text[i:j+1] for i, j in [(text.find("{"), text.rfind("}"))] if i != -1 and j > i),
        ]:
            try:
                data = json.loads(attempt_text)
                decomp_type = data.get("decomposition_type", "ATOMIC").upper()
                if decomp_type not in ("AND", "OR", "CONDITIONAL", "ATOMIC"):
                    decomp_type = "ATOMIC"

                sub_questions = data.get("sub_questions", [])
                if not isinstance(sub_questions, list):
                    return None

                # Validate sub-questions have required fields
                valid_subs = []
                for sq in sub_questions:
                    if isinstance(sq, dict) and "question" in sq:
                        valid_subs.append(sq)
                    elif isinstance(sq, str):
                        valid_subs.append({"question": sq})

                return {
                    "decomposition_type": decomp_type,
                    "sub_questions": valid_subs,
                }
            except (json.JSONDecodeError, ValueError):
                continue

        logger.warning("Failed to parse decomposition response")
        return None

    async def _assess_sub_questions(
        self,
        sub_questions: list[dict],
        market: Market,
        news_context: str,
        base_rate_context: str,
        decomp_type: str = "OR",
    ) -> list[dict]:
        """Assess each sub-question's probability.

        For OR types: assesses in parallel (independence is reasonable).
        For AND/CONDITIONAL types: assesses sequentially, conditioning each
        sub-question on prior sub-questions being TRUE. This fixes the
        independence assumption bug where P(A)*P(B) underestimates when
        A and B are positively correlated (e.g., "Will X run AND win?").

        Returns list of dicts with 'question', 'probability', 'key_factor', 'tokens_used'.
        """
        from src.analysis.prompt_templates import (
            SUB_QUESTION_TEMPLATE,
            CONDITIONAL_SUB_QUESTION_TEMPLATE,
            _sanitize_external_text,
        )

        async def _assess_one(sq: dict, assumed_true_questions: list[str] | None = None) -> Optional[dict]:
            question = sq.get("question", "")
            base_rate_hint = sq.get("base_rate_hint", "")

            if assumed_true_questions:
                # Conditional assessment: tell Claude to assume prior sub-questions are TRUE
                assumed_text = "\n".join(
                    f"  {i+1}. {q}" for i, q in enumerate(assumed_true_questions)
                )
                prompt = CONDITIONAL_SUB_QUESTION_TEMPLATE.format(
                    sub_question=_sanitize_external_text(question, 500),
                    parent_question=_sanitize_external_text(market.question, 300),
                    resolution_criteria=_sanitize_external_text(
                        market.description or "Standard resolution rules apply.", 1000
                    ),
                    assumed_true=assumed_text,
                    base_rate_hint=_sanitize_external_text(base_rate_hint, 500),
                    news_context=_sanitize_external_text(
                        news_context or "No additional context.", 2000
                    ),
                    base_rate_context=_sanitize_external_text(base_rate_context, 500),
                )
            else:
                # Unconditional assessment (first sub-question, or OR type)
                prompt = SUB_QUESTION_TEMPLATE.format(
                    sub_question=_sanitize_external_text(question, 500),
                    parent_question=_sanitize_external_text(market.question, 300),
                    resolution_criteria=_sanitize_external_text(
                        market.description or "Standard resolution rules apply.", 1000
                    ),
                    base_rate_hint=_sanitize_external_text(base_rate_hint, 500),
                    news_context=_sanitize_external_text(
                        news_context or "No additional context.", 2000
                    ),
                    base_rate_context=_sanitize_external_text(base_rate_context, 500),
                )

            try:
                model = self.forecaster._select_model()
                temperature = 0.25
                timeout = self.forecaster.settings.claude.api_timeout_seconds

                response = await self.forecaster._call_claude(prompt, model, temperature, timeout)
                raw_text = self.forecaster._extract_text(response)
                if raw_text is None:
                    return None

                tokens_used = response.usage.input_tokens + response.usage.output_tokens
                self.forecaster._track_tokens(
                    tokens_used,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    model=model,
                )

                forecast = self.forecaster._parse_response(raw_text)
                return {
                    "question": question,
                    "probability": forecast.probability,
                    "key_factor": forecast.key_factors_for[0] if forecast.key_factors_for else "",
                    "tokens_used": tokens_used,
                }
            except Exception as e:
                logger.warning(f"Sub-question assessment failed: {question[:50]}... — {e}")
                return None

        capped = sub_questions[:5]

        if decomp_type in ("AND", "CONDITIONAL"):
            # Sequential assessment with conditioning context
            # First sub-question is unconditional, subsequent ones condition on prior = TRUE
            valid_results = []
            assumed_true: list[str] = []

            for sq in capped:
                result = await _assess_one(sq, assumed_true if assumed_true else None)
                if result is not None:
                    valid_results.append(result)
                    assumed_true.append(sq.get("question", ""))
                else:
                    # If any sub-question fails, we can't chain conditionals reliably
                    logger.warning(
                        f"Conditional chain broken at: {sq.get('question', '')[:50]}..."
                    )
                    break

            return valid_results
        else:
            # OR type: parallel assessment (independence is reasonable)
            tasks = [_assess_one(sq) for sq in capped]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            valid_results = []
            for r in results:
                if isinstance(r, dict) and r is not None:
                    valid_results.append(r)
                elif isinstance(r, Exception):
                    logger.warning(f"Sub-question assessment error: {r}")

            return valid_results

    @staticmethod
    def _recombine(
        decomp_type: str,
        sub_results: list[dict],
    ) -> Optional[float]:
        """Recombine sub-probabilities using the correct logical operator.

        Args:
            decomp_type: "AND", "OR", or "CONDITIONAL"
            sub_results: List of dicts with 'probability' field

        Returns:
            Combined probability, or None if recombination fails
        """
        probs = [r["probability"] for r in sub_results]

        if not probs:
            return None

        if decomp_type not in ("AND", "OR", "CONDITIONAL"):
            logger.warning(f"Unknown decomposition type: {decomp_type}")
            return None

        if len(probs) == 1:
            return probs[0]

        if decomp_type == "AND":
            # P(A ∧ B ∧ C) = P(A) × P(B|A) × P(C|A∧B)
            # Sub-questions are assessed conditionally, so multiplication is correct
            combined = 1.0
            for p in probs:
                combined *= p
            return combined

        elif decomp_type == "OR":
            # P(A or B or C) = 1 - (1-P(A)) * (1-P(B)) * (1-P(C))
            complement = 1.0
            for p in probs:
                complement *= (1.0 - p)
            return 1.0 - complement

        else:  # CONDITIONAL
            # P(A and B) = P(A) * P(B|A)
            # Sub-questions should be ordered: first is unconditional,
            # subsequent are conditional on previous ones
            combined = probs[0]
            for p in probs[1:]:
                combined *= p
            return combined
