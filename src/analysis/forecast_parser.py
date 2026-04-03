"""Response parsing for Claude forecaster.

Handles JSON extraction from Claude API responses, including fallback
strategies for malformed output. Extracted from claude_forecaster.py
for modularity (M-1 audit item).
"""

from __future__ import annotations

import json
import logging
import re

from src.core.models import ForecastResult

logger = logging.getLogger(__name__)


def extract_text(response) -> str | None:
    """Safely extract text from Claude API response. Returns None if empty."""
    if not response.content:
        return None
    return response.content[0].text


def parse_response(raw_text: str) -> ForecastResult:
    """Parse Claude's JSON response into a ForecastResult.

    Tries multiple strategies to extract JSON:
    1. Direct parse of the full response
    2. Extract from markdown code blocks (```json ... ```)
    3. Find first { and last } and parse that substring
    4. Fall back to 0.5 only as last resort
    """
    text = raw_text.strip()

    # Strategy 1: Direct parse
    try:
        data = json.loads(text)
        return build_forecast(data)
    except (json.JSONDecodeError, ValueError) as e:
        logger.debug(f"JSON direct parse failed, trying fallbacks: {e}")

    # Strategy 2: Extract from markdown code blocks
    code_block_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if code_block_match:
        try:
            data = json.loads(code_block_match.group(1).strip())
            return build_forecast(data)
        except (json.JSONDecodeError, ValueError) as e:
            logger.debug(f"JSON code block parse failed: {e}")

    # Strategy 3: Find first { and last } and try to parse
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        try:
            data = json.loads(text[first_brace:last_brace + 1])
            return build_forecast(data)
        except (json.JSONDecodeError, ValueError) as e:
            logger.debug(f"JSON brace extraction parse failed: {e}")

    # Strategy 4: Try to extract probability from prose as last resort.
    # Use findall + take LAST match to avoid picking up stale references
    # like "probability shifted from 0.73 to 0.85" (we want 0.85, not 0.73).
    prob_matches = re.findall(
        r'(?:probability|prob)["\'\s:=]+\s*([01]?\.\d+|0|1(?:\.0+)?)', text, re.IGNORECASE
    )
    if not prob_matches:
        # Try percentage format: "probability: 65%"
        pct_matches = re.findall(r'(?:probability|prob)["\'\s:=]+\s*(\d{1,3})%', text, re.IGNORECASE)
        if pct_matches:
            prob = float(pct_matches[-1]) / 100.0
            parse_failed = True
            # M-2: Validate extraction context — check surrounding sentence
            parse_failed = _validate_prose_extraction(text, prob, parse_failed)
            # M-2: Check for ambiguous numbers (large spread among extracted values)
            all_vals = [float(v) / 100.0 for v in pct_matches]
            if len(all_vals) > 1 and (max(all_vals) - min(all_vals)) > 0.30:
                logger.warning(
                    f"Ambiguous prose extraction: values span {min(all_vals):.2f}–{max(all_vals):.2f} "
                    f"(delta {max(all_vals) - min(all_vals):.2f} > 0.30)"
                )
                parse_failed = True
            logger.warning(f"Extracted probability {prob} from percentage in prose (last of {len(pct_matches)} matches)")
            return ForecastResult(
                probability=max(0.01, min(0.99, prob)),
                reasoning=f"Parsed probability from prose (%). Raw: {raw_text[:200]}",
                parse_failed=parse_failed,
            )
    if prob_matches:
        prob = float(prob_matches[-1])
        parse_failed = True
        # M-2: Validate extraction context — check surrounding sentence
        parse_failed = _validate_prose_extraction(text, prob, parse_failed)
        # M-2: Check for ambiguous numbers (large spread among extracted values)
        all_vals = [float(v) for v in prob_matches]
        if len(all_vals) > 1 and (max(all_vals) - min(all_vals)) > 0.30:
            logger.warning(
                f"Ambiguous prose extraction: values span {min(all_vals):.2f}–{max(all_vals):.2f} "
                f"(delta {max(all_vals) - min(all_vals):.2f} > 0.30)"
            )
            parse_failed = True
        logger.warning(f"Extracted probability {prob} from prose response (last of {len(prob_matches)} matches)")
        return ForecastResult(
            probability=max(0.01, min(0.99, prob)),
            reasoning=f"Parsed probability from prose. Raw: {raw_text[:200]}",
            parse_failed=parse_failed,
        )

    logger.warning(f"Failed to parse Claude response as JSON: {raw_text[:200]}")
    return ForecastResult(
        probability=0.5,
        reasoning=f"JSON parse failed, raw: {raw_text[:200]}",
        parse_failed=True,
    )


def _validate_prose_extraction(text: str, prob: float, parse_failed: bool) -> bool:
    """M-2: Validate that extracted probability appears in a forecasting context.

    Returns updated parse_failed flag (may be set to True if context is suspect).
    """
    # Check if the extracted value appears near forecasting-related words
    context_words = {"probability", "estimate", "likely", "chance", "forecast", "predict", "assessment"}
    # Search for any sentence containing the extracted number
    # Build a pattern matching the number as decimal or percentage
    text_lower = text.lower()
    has_context = any(word in text_lower for word in context_words)
    if not has_context:
        logger.warning(
            f"Prose extraction lacks forecasting context words — "
            f"extracted {prob} may not be a probability estimate"
        )
        parse_failed = True
    return parse_failed


def build_forecast(data: dict) -> ForecastResult:
    """Build a ForecastResult from parsed JSON data."""
    # H-11: Validate that a probability key exists; check common alternatives
    if "probability" not in data:
        alt_keys = {"prob": None, "p": None, "forecast": None, "prediction": None}
        found_key = None
        for alt in alt_keys:
            if alt in data:
                found_key = alt
                break
        if found_key is not None:
            logger.warning(
                f"JSON schema validation: 'probability' key missing, "
                f"using alternative key '{found_key}' = {data[found_key]}"
            )
            data["probability"] = data[found_key]
        else:
            logger.warning(
                "JSON schema validation: 'probability' key missing and no "
                f"known alternatives found in keys: {list(data.keys())}"
            )
            return ForecastResult(
                probability=0.5,
                reasoning=f"Missing 'probability' key in JSON. Keys: {list(data.keys())}",
                parse_failed=True,
            )

    raw_probability = float(data.get("probability", 0.5))
    probability = max(0.01, min(0.99, raw_probability))

    if raw_probability != probability:
        logger.debug(
            f"Clamped probability from {raw_probability:.6f} to {probability:.2f}"
        )

    # Safe CI extraction with fallback defaults
    def _safe_float(value, default: float) -> float:
        """Safely convert to float, returning default on failure."""
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            logger.warning(f"Non-numeric CI value: {value!r} — using default {default}")
            return default

    ci_low_raw = _safe_float(data.get("confidence_low"), max(0, probability - 0.20))
    ci_high_raw = _safe_float(data.get("confidence_high"), min(1, probability + 0.20))

    return ForecastResult(
        probability=probability,
        confidence_low=max(0.0, min(1.0, ci_low_raw)),
        confidence_high=max(0.0, min(1.0, ci_high_raw)),
        key_factors_for=data.get("key_factors_for", []),
        key_factors_against=data.get("key_factors_against", []),
        uncertainties=data.get("uncertainties", []),
        reasoning=data.get("reasoning", ""),
    )
