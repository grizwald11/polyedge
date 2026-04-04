"""Prompt Variant A/B Testing Framework — Thompson sampling for prompt optimization.

Manages multiple prompt variants per market category and uses Thompson sampling
(Beta distribution) to balance exploration vs exploitation. Over time, the
system automatically converges on the best-performing prompt variant for each
category based on Brier score performance.

This creates a compounding improvement loop: better prompts → better predictions
→ better Brier scores → more traffic to better prompts.

Feature 7 enhancement: outcomes are validated against actual market resolutions
(not just approximated from market price). Pending predictions are tracked per
variant and resolved when the market settles, feeding real Brier scores back
into Thompson sampling. State is persisted to the database across restarts.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from src.core.models import MarketCategory

logger = logging.getLogger(__name__)


@dataclass
class VariantRecord:
    """Tracks performance of a single prompt variant."""

    name: str
    # Thompson sampling uses Beta(alpha, beta) distribution.
    # alpha ~ "successes" (good predictions), beta ~ "failures" (bad predictions).
    # Start with weak prior (1, 1) = uniform distribution.
    alpha: float = 1.0
    beta: float = 1.0
    total_predictions: int = 0
    sum_brier: float = 0.0  # Sum of Brier scores for average calculation

    @property
    def avg_brier(self) -> Optional[float]:
        if self.total_predictions == 0:
            return None
        return self.sum_brier / self.total_predictions

    def sample(self) -> float:
        """Draw a sample from the Beta distribution (Thompson sampling).

        Higher sample = variant is believed to be better.
        We convert Brier (lower=better) to a "quality" score (higher=better)
        by using alpha = good outcomes, beta = bad outcomes.
        """
        return random.betavariate(self.alpha, self.beta)

    def update(self, brier_score: float) -> None:
        """Update the variant's performance with a new Brier score observation.

        Brier scores range from 0 (perfect) to 1 (worst). We convert this
        to a success/failure signal for the Beta distribution:
        - Brier < 0.15 → "success" (alpha += 1)
        - Brier > 0.25 → "failure" (beta += 1)
        - In between → fractional update

        Args:
            brier_score: The Brier score for this prediction (0-1)
        """
        self.total_predictions += 1
        self.sum_brier += brier_score

        # Convert Brier to success signal: 1.0 = perfect, 0.0 = terrible
        quality = max(0.0, min(1.0, 1.0 - brier_score * 4.0))
        # quality mapping: brier=0 → 1.0, brier=0.15 → 0.4, brier=0.25 → 0.0

        self.alpha += quality
        self.beta += (1.0 - quality)


# ─── Prompt modifiers ───
# Each modifier takes the base template and returns a modified version.
# They don't replace the template entirely — they augment it.


def _modifier_control(template: str) -> str:
    """Control variant — no modification."""
    return template


def _modifier_explicit_base_rate(template: str) -> str:
    """Prepend explicit base-rate anchoring instruction."""
    insert = (
        "\nIMPORTANT: Before considering specific evidence, first state your "
        "base rate estimate for this type of event based on historical frequency. "
        "Then explicitly adjust from that base rate using the evidence provided.\n"
    )
    # Insert after the first line (usually "Assess the probability...")
    lines = template.split("\n", 1)
    if len(lines) == 2:
        return lines[0] + insert + lines[1]
    return insert + template


def _modifier_devils_advocate(template: str) -> str:
    """Append devil's advocate instruction before JSON request."""
    insert = (
        "\nAFTER forming your initial probability estimate, argue the strongest "
        "possible case for the OPPOSITE outcome. Consider what evidence or "
        "developments would prove your initial estimate wrong. Then provide your "
        "FINAL adjusted probability estimate in the JSON response.\n"
    )
    # Insert before "Provide your probability estimate as JSON."
    marker = "Provide your probability estimate as JSON."
    if marker in template:
        return template.replace(marker, insert + marker)
    return template + insert


def _modifier_no_market_price(template: str) -> str:
    """Remove current market price to test anchoring effects."""
    import re
    # Remove the CURRENT MARKET PRICE line
    modified = re.sub(r"CURRENT MARKET PRICE:.*\n", "", template)
    return modified


# Registry of available modifiers
VARIANT_MODIFIERS: dict[str, Callable[[str], str]] = {
    "control": _modifier_control,
    "explicit_base_rate": _modifier_explicit_base_rate,
    "devils_advocate": _modifier_devils_advocate,
    "no_market_price": _modifier_no_market_price,
}


class PromptVariantManager:
    """Manages prompt variants and selects the best one using Thompson sampling.

    Usage:
        manager = PromptVariantManager()
        variant_name, modified_template = manager.select_variant(category, base_template)
        # ... use modified_template for Claude call ...
        # After resolution:
        manager.record_outcome(category, variant_name, brier_score)
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        # Per-category variant performance: {category: {variant_name: VariantRecord}}
        self._records: dict[str, dict[str, VariantRecord]] = {}
        # Pending predictions awaiting resolution: {category: {market_id: {variant_name, predicted_prob}}}
        self._pending_predictions: dict[str, dict[str, dict]] = {}

    def _ensure_category(self, category: str) -> dict[str, VariantRecord]:
        """Ensure all variants are registered for a category."""
        if category not in self._records:
            self._records[category] = {
                name: VariantRecord(name=name)
                for name in VARIANT_MODIFIERS
            }
        return self._records[category]

    def select_variant(
        self,
        category: MarketCategory,
        base_template: str,
    ) -> tuple[str, str]:
        """Select a prompt variant using Thompson sampling.

        Args:
            category: Market category
            base_template: The base prompt template for this category

        Returns:
            Tuple of (variant_name, modified_template)
        """
        if not self.enabled:
            return "control", base_template

        cat_key = category.value
        variants = self._ensure_category(cat_key)

        # Thompson sampling: draw from each variant's Beta distribution,
        # select the one with the highest sample
        best_name = "control"
        best_sample = -1.0

        for name, record in variants.items():
            sample = record.sample()
            if sample > best_sample:
                best_sample = sample
                best_name = name

        modifier = VARIANT_MODIFIERS.get(best_name, _modifier_control)
        modified_template = modifier(base_template)

        logger.debug(
            f"A/B test: selected variant '{best_name}' for {cat_key} "
            f"(sample={best_sample:.3f})"
        )

        return best_name, modified_template

    def record_outcome(
        self,
        category: MarketCategory,
        variant_name: str,
        brier_score: float,
    ) -> None:
        """Record a prediction outcome for a variant.

        Args:
            category: Market category
            variant_name: Which variant was used
            brier_score: The Brier score for this prediction
        """
        cat_key = category.value
        variants = self._ensure_category(cat_key)

        record = variants.get(variant_name)
        if record is None:
            logger.warning(f"Unknown variant '{variant_name}' for {cat_key}")
            return

        record.update(brier_score)
        logger.debug(
            f"A/B test: recorded brier={brier_score:.3f} for '{variant_name}' "
            f"in {cat_key} (n={record.total_predictions}, "
            f"avg_brier={record.avg_brier:.3f})"
        )

    def get_variant_stats(self) -> dict[str, dict[str, dict]]:
        """Get performance statistics for all variants across categories.

        Returns:
            Nested dict: {category: {variant: {avg_brier, n, alpha, beta}}}
        """
        stats = {}
        for cat_key, variants in self._records.items():
            stats[cat_key] = {}
            for name, record in variants.items():
                stats[cat_key][name] = {
                    "avg_brier": record.avg_brier,
                    "total_predictions": record.total_predictions,
                    "alpha": record.alpha,
                    "beta": record.beta,
                }
        return stats

    def record_prediction(
        self,
        category: MarketCategory,
        variant_name: str,
        market_id: str,
        predicted_prob: float,
    ) -> None:
        """Record a pending prediction for outcome-validated scoring.

        Called when a forecast is made. The prediction is stored until the
        market resolves, at which point record_actual_outcome() computes
        the real Brier score and updates Thompson sampling params.
        """
        cat_key = category.value
        if cat_key not in self._pending_predictions:
            self._pending_predictions[cat_key] = {}
        # Cap pending predictions per category to prevent unbounded growth
        _MAX_PENDING_PER_CAT = 500
        if len(self._pending_predictions[cat_key]) >= _MAX_PENDING_PER_CAT:
            # Drop oldest half
            keys = list(self._pending_predictions[cat_key].keys())
            for k in keys[: len(keys) // 2]:
                del self._pending_predictions[cat_key][k]
        # Store by market_id; if multiple predictions per market, keep latest
        self._pending_predictions[cat_key][market_id] = {
            "variant_name": variant_name,
            "predicted_prob": predicted_prob,
        }

    def record_actual_outcome(
        self,
        market_id: str,
        actual_outcome: bool,
    ) -> int:
        """Record the actual market resolution and update Thompson params.

        Searches all categories for pending predictions on this market_id,
        computes the real Brier score, and updates the variant's Thompson
        sampling parameters.

        Returns:
            Number of variant records updated.
        """
        outcome_val = 1.0 if actual_outcome else 0.0
        updated = 0

        for cat_key, pending in self._pending_predictions.items():
            if market_id not in pending:
                continue

            entry = pending.pop(market_id)
            variant_name = entry["variant_name"]
            predicted_prob = entry["predicted_prob"]

            # Real Brier score: (predicted - actual)^2
            brier = (predicted_prob - outcome_val) ** 2

            variants = self._ensure_category(cat_key)
            record = variants.get(variant_name)
            if record is not None:
                record.update(brier)
                updated += 1
                logger.info(
                    f"A/B outcome: variant '{variant_name}' in {cat_key} — "
                    f"pred={predicted_prob:.2f}, actual={'YES' if actual_outcome else 'NO'}, "
                    f"brier={brier:.3f} (n={record.total_predictions})"
                )

        return updated

    def save_state(self, db) -> None:
        """Persist Thompson sampling state to the database.

        Args:
            db: Database instance with _get_conn() method.
        """
        conn = db._get_conn()
        now = datetime.now(timezone.utc).isoformat()

        for cat_key, variants in self._records.items():
            for name, record in variants.items():
                conn.execute("""
                    INSERT INTO prompt_variant_stats
                        (category, variant_name, alpha, beta, total_predictions, sum_brier, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(category, variant_name) DO UPDATE SET
                        alpha=excluded.alpha,
                        beta=excluded.beta,
                        total_predictions=excluded.total_predictions,
                        sum_brier=excluded.sum_brier,
                        updated_at=excluded.updated_at
                """, (cat_key, name, record.alpha, record.beta,
                      record.total_predictions, record.sum_brier, now))

        conn.commit()
        logger.debug(f"Saved A/B testing state ({sum(len(v) for v in self._records.values())} records)")

    def load_state(self, db) -> None:
        """Load Thompson sampling state from the database.

        Args:
            db: Database instance with _get_conn() method.
        """
        try:
            conn = db._get_conn()
            rows = conn.execute("SELECT * FROM prompt_variant_stats").fetchall()
        except Exception as e:
            logger.debug(f"Could not load A/B testing state: {e}")
            return

        loaded = 0
        for row in rows:
            cat_key = row["category"]
            name = row["variant_name"]

            # Only load known variants
            if name not in VARIANT_MODIFIERS:
                continue

            variants = self._ensure_category(cat_key)
            if name in variants:
                record = variants[name]
                record.alpha = row["alpha"]
                record.beta = row["beta"]
                record.total_predictions = row["total_predictions"]
                record.sum_brier = row["sum_brier"]
                loaded += 1

        if loaded:
            logger.info(f"Loaded A/B testing state: {loaded} variant records")

    def check_significance(self, min_samples: int = 30) -> dict[str, Optional[str]]:
        """Check if any variant is significantly better per category.

        Returns dict of {category: winning_variant_name or None}.
        A variant "wins" if it has min_samples and its average Brier is
        at least 0.02 better than the second-best variant.
        """
        winners = {}
        for cat_key, variants in self._records.items():
            eligible = [
                (name, record)
                for name, record in variants.items()
                if record.total_predictions >= min_samples and record.avg_brier is not None
            ]

            if len(eligible) < 2:
                winners[cat_key] = None
                continue

            # Sort by avg Brier (lower is better)
            eligible.sort(key=lambda x: x[1].avg_brier)
            best_name, best_record = eligible[0]
            second_name, second_record = eligible[1]

            margin = second_record.avg_brier - best_record.avg_brier
            if margin >= 0.02:
                winners[cat_key] = best_name
                logger.info(
                    f"A/B test winner for {cat_key}: '{best_name}' "
                    f"(brier={best_record.avg_brier:.3f}) beats '{second_name}' "
                    f"(brier={second_record.avg_brier:.3f}) by {margin:.3f}"
                )
            else:
                winners[cat_key] = None

        return winners
