"""Pre-mortem adversarial analyzer — forces counterargument reasoning.

After an initial forecast, asks Claude to imagine the OPPOSITE outcome
occurred and write a plausible narrative. Strong counterarguments widen
the confidence interval and shift the probability toward 0.5.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from src.core.models import ForecastResult, Market

logger = logging.getLogger(__name__)


ADVERSARIAL_TEMPLATE = """PRE-MORTEM ANALYSIS

You previously estimated this market at {probability:.0%} probability of resolving YES.

MARKET: {question}
RESOLUTION CRITERIA: {resolution_criteria}

YOUR INITIAL ESTIMATE: {probability:.0%} YES

TASK: Imagine it is resolution day and the market resolved {opposite_outcome}. Write a specific, plausible narrative explaining what happened. What events occurred? What did you get wrong? What assumption was your weakest?

Then rate how plausible your own counter-narrative is on a scale of 0-100:
- 0 = completely implausible, initial estimate was nearly certain
- 50 = reasonable alternative, could go either way
- 100 = very plausible, initial estimate was overconfident

CRITICAL: Respond ONLY with valid JSON.
{{"counter_narrative": "<specific narrative of opposite outcome>", "plausibility": <int 0-100>, "weakest_assumption": "<the single weakest assumption in the initial forecast>", "key_risks": ["<risk 1>", "<risk 2>"]}}"""


@dataclass
class AdversarialResult:
    """Result of a pre-mortem adversarial analysis."""
    counter_narrative: str = ""
    plausibility: float = 0.0  # 0-1 normalized
    weakest_assumption: str = ""
    key_risks: list[str] = None
    ci_adjustment: float = 0.0  # How much to widen CI on each side
    probability_adjustment: float = 0.0  # Shift toward 0.5

    def __post_init__(self):
        if self.key_risks is None:
            self.key_risks = []


class AdversarialAnalyzer:
    """Runs pre-mortem adversarial analysis on forecasts."""

    # Only run adversarial check when edge is significant and CI is tight
    MIN_EDGE_FOR_CHECK = 0.08
    MAX_CI_WIDTH_FOR_CHECK = 0.30

    def __init__(self, forecaster):
        self.forecaster = forecaster

    def should_run(self, edge: float, ci_width: float) -> bool:
        """Check if adversarial analysis should run for this forecast."""
        return abs(edge) >= self.MIN_EDGE_FOR_CHECK and ci_width < self.MAX_CI_WIDTH_FOR_CHECK

    async def run_premortem(
        self,
        market: Market,
        forecast: ForecastResult,
    ) -> AdversarialResult:
        """Run pre-mortem analysis: imagine the opposite outcome.

        Returns AdversarialResult with adjustment recommendations.
        """
        opposite = "NO" if forecast.probability > 0.5 else "YES"

        prompt = ADVERSARIAL_TEMPLATE.format(
            question=market.question[:500],
            resolution_criteria=(market.description or "Standard resolution")[:2000],
            probability=forecast.probability,
            opposite_outcome=opposite,
        )

        try:
            result = await self.forecaster.assess_market_with_prompt(
                market=market,
                custom_prompt=prompt,
                position_value=0.0,
            )
            if result is None or getattr(result, "parse_failed", False):
                return AdversarialResult()

            return self._parse_result(result.raw_response or "", forecast.probability)

        except Exception as e:
            logger.debug(f"Pre-mortem analysis failed for {market.ticker}: {e}")
            return AdversarialResult()

    def _parse_result(self, raw_text: str, initial_prob: float) -> AdversarialResult:
        """Parse Claude's response and compute adjustments."""
        try:
            data = json.loads(raw_text.strip())
        except json.JSONDecodeError:
            match = re.search(r'\{.*\}', raw_text, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    return AdversarialResult()
            else:
                return AdversarialResult()

        plausibility_raw = data.get("plausibility", 0)
        plausibility = max(0.0, min(1.0, float(plausibility_raw) / 100.0))

        # Compute adjustments based on plausibility
        # High plausibility → shift toward 0.5 and widen CI
        # Scaling: plausibility 0.7+ triggers meaningful adjustment
        if plausibility > 0.7:
            ci_adj = 0.05 + (plausibility - 0.7) * 0.33  # 0.05-0.15
            prob_adj = (0.5 - initial_prob) * (plausibility - 0.5) * 0.4  # Shift toward 0.5
        elif plausibility > 0.5:
            ci_adj = 0.03
            prob_adj = (0.5 - initial_prob) * (plausibility - 0.5) * 0.2
        else:
            ci_adj = 0.0
            prob_adj = 0.0

        return AdversarialResult(
            counter_narrative=data.get("counter_narrative", ""),
            plausibility=plausibility,
            weakest_assumption=data.get("weakest_assumption", ""),
            key_risks=data.get("key_risks", [])[:5],
            ci_adjustment=round(ci_adj, 3),
            probability_adjustment=round(prob_adj, 3),
        )

    def apply_adjustment(
        self,
        forecast: ForecastResult,
        adversarial: AdversarialResult,
    ) -> ForecastResult:
        """Apply adversarial adjustments to the forecast.

        Returns a new ForecastResult with widened CI and adjusted probability.
        """
        if adversarial.ci_adjustment == 0 and adversarial.probability_adjustment == 0:
            return forecast

        new_prob = max(0.01, min(0.99,
            forecast.probability + adversarial.probability_adjustment
        ))
        new_ci_low = max(0.0, forecast.confidence_low - adversarial.ci_adjustment)
        new_ci_high = min(1.0, forecast.confidence_high + adversarial.ci_adjustment)

        # Ensure CI still contains the probability
        new_ci_low = min(new_ci_low, new_prob - 0.01)
        new_ci_high = max(new_ci_high, new_prob + 0.01)

        logger.info(
            f"Adversarial adjustment: prob {forecast.probability:.0%}→{new_prob:.0%}, "
            f"CI [{forecast.confidence_low:.0%},{forecast.confidence_high:.0%}]→"
            f"[{new_ci_low:.0%},{new_ci_high:.0%}] "
            f"(plausibility={adversarial.plausibility:.0%})"
        )

        return ForecastResult(
            probability=new_prob,
            confidence_low=new_ci_low,
            confidence_high=new_ci_high,
            key_factors_for=forecast.key_factors_for,
            key_factors_against=forecast.key_factors_against + adversarial.key_risks,
            uncertainties=forecast.uncertainties + [adversarial.weakest_assumption] if adversarial.weakest_assumption else forecast.uncertainties,
            reasoning=forecast.reasoning,
            model_used=forecast.model_used,
            tokens_used=forecast.tokens_used,
            latency_ms=forecast.latency_ms,
            raw_response=forecast.raw_response,
        )
