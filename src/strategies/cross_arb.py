"""Cross-market arbitrage strategy — detects logical pricing inconsistencies.

Three arbitrage types:
- Type A (intra-market): YES + NO prices sum to < 0.98
- Type B (logical/subset): Claude-validated subset/superset relationships
- Type C (mutual exclusivity): Multi-outcome event prices don't sum to 100%
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.analysis.claude_forecaster import ClaudeForecaster
from src.analysis.prompt_templates import ARB_VALIDATION_TEMPLATE
from src.config import Settings
from src.core.models import (
    Direction,
    Market,
    Signal,
    StrategyName,
)
from src.data.market_graph import MarketGraph
from src.storage.database import Database

logger = logging.getLogger(__name__)


class CrossArbStrategy:
    """Detects and trades logical pricing inconsistencies between related markets."""

    def __init__(
        self,
        market_graph: MarketGraph,
        forecaster: ClaudeForecaster,
        settings: Settings,
        db: Database,
    ):
        self.graph = market_graph
        self.forecaster = forecaster
        self.settings = settings
        self.db = db
        self.min_edge = settings.trading.min_edge_arb

    async def scan_for_opportunities(
        self, markets: list[Market]
    ) -> list[Signal]:
        """Scan all markets for arbitrage opportunities.

        Returns signals for Type A, B, and C arbitrage.
        """
        signals: list[Signal] = []

        # Type A: Intra-market rebalancing
        for market in markets:
            signal = self._check_intra_market(market)
            if signal:
                signals.append(signal)

        # Type C: Mutual exclusivity sum check (grouped by event)
        event_markets: dict[str, list[Market]] = {}
        for market in markets:
            if market.event_ticker:
                event_markets.setdefault(market.event_ticker, []).append(market)

        for event_ticker, event_mkts in event_markets.items():
            if len(event_mkts) >= 2:
                event_signals = self._check_mutual_exclusivity(event_mkts, event_ticker)
                signals.extend(event_signals)

        # Type B: Logical/subset arbitrage via market graph
        subset_signals = await self._check_subset_arb(markets)
        signals.extend(subset_signals)

        if signals:
            logger.info(f"Cross-arb: {len(signals)} opportunities found")

        return signals

    def _check_intra_market(self, market: Market) -> Optional[Signal]:
        """Type A: Check if YES + NO prices sum to less than 1.0 (minus fee threshold).

        H-2: This emits a signal for the cheaper side only (single-leg directional
        trade, NOT a guaranteed-profit arbitrage). The edge comes from the market
        mispricing: if YES + NO < 1.0, the cheaper side is more likely underpriced.
        True two-leg arb would require simultaneously buying both sides, which is
        not implemented here.
        """
        if market.yes_price <= 0 or market.no_price <= 0:
            return None

        total = market.yes_price + market.no_price
        edge = 1.0 - total
        if edge < self.min_edge:
            return None

        # Buy the cheaper side — more likely to be the underpriced one
        if market.yes_price < market.no_price:
            direction = Direction.BUY_YES
            price = market.yes_price
        else:
            direction = Direction.BUY_NO
            price = market.no_price

        return Signal(
            strategy=StrategyName.CROSS_ARB,
            market_id=market.ticker,
            market_question=market.question,
            direction=direction,
            edge=edge,
            # For arb, "true probability" is irrelevant — we're exploiting math,
            # not prediction. Set probability = price + edge so Kelly derives
            # the correct market_price (probability - edge = price).
            probability_estimate=min(0.99, price + edge),
            market_price=price,
            confidence=0.9,  # High confidence — mathematical
            reasoning=f"Intra-market mispricing: YES({market.yes_price:.2f}) + NO({market.no_price:.2f}) = {total:.2f} < 1.00, buying cheaper side",
        )

    def _is_mutually_exclusive(self, markets: list[Market]) -> bool:
        """Determine if an event's outcomes are mutually exclusive.

        Mutually exclusive: exactly one outcome resolves YES (e.g., "Who will WIN?")
        Independent: multiple outcomes can resolve YES (e.g., "Will X visit country?")
        Temporal: same question with different dates (e.g., "before Apr 1" / "before Jun 1")

        Returns True only for genuinely exclusive events where sum-to-1 applies.
        """
        if len(markets) < 2:
            return False

        questions = [m.question.lower() for m in markets]

        # Temporal cascade detection: same base question with different dates.
        # These are NOT mutually exclusive (if true for Apr, also true for Jun).
        # Check if questions differ only in date-like suffixes.
        import re
        date_pattern = re.compile(
            r'(before |after |during |by |by end of |within )'
            r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|january|february|'
            r'march|april|may|june|july|august|september|october|november|december'
            r'|q[1-4]|next quarter|next month|\d+\s*months?)'
            r'[^?]*',
            re.IGNORECASE,
        )
        stripped = set()
        any_had_dates = False
        for q in questions:
            s = date_pattern.sub('', q).strip().rstrip('?').strip()
            if s != q.strip().rstrip('?').strip():
                any_had_dates = True
            stripped.add(s)
        if len(stripped) == 1 and any_had_dates:
            # All questions are the same after removing date qualifiers → temporal cascade
            return False

        # Threshold/range cascade detection: same base question with different
        # numeric thresholds (e.g., "below 38%" vs "below 36%"). These are
        # nested/cumulative — if below 36% is true, below 38% is also true.
        # NOT mutually exclusive.
        threshold_pattern = re.compile(
            r'(below |above |over |under |at least |more than |less than |fewer than )'
            r'[\d,.]+%?',
            re.IGNORECASE,
        )
        threshold_stripped = set()
        any_had_thresholds = False
        for q in questions:
            s = threshold_pattern.sub('', q).strip().rstrip('?').strip()
            if s != q.strip().rstrip('?').strip():
                any_had_thresholds = True
            threshold_stripped.add(s)
        if len(threshold_stripped) == 1 and any_had_thresholds:
            # All questions are the same after removing thresholds → nested cascade
            return False

        # Independent event keywords: each outcome asks "Will [person/thing] [verb]?"
        # where multiple can independently be true.
        independent_patterns = [
            r'\bwill .+ (visit|meet|pardon|run for|leave|attend|receive|release)\b',
            r'\bwill .+ (become|sign|announce|resign|join|endorse)\b',
        ]
        shared_question = all(q == questions[0] for q in questions)
        if not shared_question:
            # Different questions per outcome — check for independent verbs
            for pattern in independent_patterns:
                if all(re.search(pattern, q) for q in questions):
                    return False

        # If all markets share the same question text (e.g., "Who will win the race
        # for TX-35?") and outcomes are different options → likely exclusive.
        if shared_question:
            # Shared question with multiple outcomes. Check for exclusive keywords.
            q = questions[0]
            exclusive_keywords = [
                r'\bwho will win\b', r'\bwinner\b', r'\bwhich\b',
                r'\bwhat will be\b', r'\bwhat will .+ be\b',
                r'\bhow many\b', r'\bwill .+ be .+ or\b',
                r'\bcontrol\b.*\band\b',  # "House control X AND Senate control Y"
                r'\bexactly \d+\b',  # "exactly 1 senator"
                r'\bfall below\b',  # ranges like "fall below 7.60"
                r'\bat least \d+\b',
            ]
            for pattern in exclusive_keywords:
                if re.search(pattern, q):
                    return True

            # Shared question + multiple outcomes with different subtitles
            # (different people/options) but no exclusive keyword →
            # assume independent unless proven otherwise.
            # This is the conservative, safe default.
            return False

        # Different questions, no independent verb match.
        # Check for structural exclusivity (e.g., combo markets, range brackets).
        # For safety, default to non-exclusive for > 2 outcomes.
        if len(markets) > 2:
            return False

        # Binary (2 outcomes): check if they look complementary.
        # Do NOT default to True — false positives here generate bad arb signals.
        if len(markets) == 2:
            q0, q1 = questions[0], questions[1]
            # Check if one is the negation or complement of the other
            if ('democratic' in q0 and 'republican' in q1) or ('republican' in q0 and 'democratic' in q1):
                return True
            if ('yes' in q0 and 'no' in q1) or ('no' in q0 and 'yes' in q1):
                return True
            # Two differently-phrased questions — NOT safe to assume exclusive.
            # Could be nested thresholds, temporal variants, or independent events.
            return False

        return False

    def _check_mutual_exclusivity(
        self, markets: list[Market], event_ticker: str
    ) -> list[Signal]:
        """Type C: Check if sum of YES prices in a multi-outcome event != 100%.

        Only applies to truly mutually exclusive events where exactly one
        outcome resolves YES. Independent events (where multiple outcomes
        can be true) are skipped.
        """
        if len(markets) < 2:
            return []

        if not self._is_mutually_exclusive(markets):
            return []

        # Skip events with stale/zero prices — these create false edge signals
        valid_markets = [m for m in markets if m.yes_price > 0]
        if len(valid_markets) < 2:
            return []

        yes_sum = sum(m.yes_price for m in valid_markets)
        markets = valid_markets  # Use only markets with valid prices

        # If sum > 1.0 + threshold: sell overpriced outcomes
        # If sum < 1.0 - threshold: buy all outcomes for guaranteed profit
        signals: list[Signal] = []

        if yes_sum < 1.0 - self.min_edge:
            basket_edge = 1.0 - yes_sum
            # Buy the cheapest outcome (best risk/reward)
            cheapest = min(markets, key=lambda m: m.yes_price)
            # Scale edge proportionally: this single outcome captures only its
            # share of the basket mispricing, preventing Kelly from oversizing.
            single_edge = basket_edge * (cheapest.yes_price / yes_sum) if yes_sum > 0 else basket_edge
            # Only emit signal if scaled edge still exceeds min threshold
            if single_edge >= self.min_edge:
                signals.append(Signal(
                    strategy=StrategyName.CROSS_ARB,
                    market_id=cheapest.ticker,
                    market_question=cheapest.question,
                    direction=Direction.BUY_YES,
                    edge=single_edge,
                    probability_estimate=min(0.99, cheapest.yes_price + single_edge),
                    market_price=cheapest.yes_price,
                    confidence=0.85,
                    reasoning=(
                        f"Mutual exclusivity arb: {event_ticker} YES prices sum "
                        f"to {yes_sum:.2f} < 1.00 ({len(markets)} outcomes), "
                        f"basket edge={basket_edge:.2f}"
                    ),
                ))

        elif yes_sum > 1.0 + self.min_edge:
            basket_edge = yes_sum - 1.0
            # Sell (buy NO on) the most expensive outcome
            most_expensive = max(markets, key=lambda m: m.yes_price)
            # Scale edge: this outcome's share of overpricing
            single_edge = basket_edge * (most_expensive.yes_price / yes_sum) if yes_sum > 0 else basket_edge
            if single_edge >= self.min_edge:
                signals.append(Signal(
                    strategy=StrategyName.CROSS_ARB,
                    market_id=most_expensive.ticker,
                    market_question=most_expensive.question,
                    direction=Direction.BUY_NO,
                    edge=single_edge,
                    probability_estimate=min(0.99, (1.0 - most_expensive.yes_price) + single_edge),
                    market_price=most_expensive.no_price,
                    confidence=0.85,
                    reasoning=(
                        f"Mutual exclusivity arb: {event_ticker} YES prices sum "
                        f"to {yes_sum:.2f} > 1.00 ({len(markets)} outcomes), "
                        f"basket edge={basket_edge:.2f}"
                    ),
                ))

        return signals

    async def _check_subset_arb(self, markets: list[Market]) -> list[Signal]:
        """Type B: Use market graph + Claude to find subset/superset mispricing."""
        signals: list[Signal] = []

        # Find potential pairs via market graph
        pairs = self.graph.find_subset_superset_pairs(markets, similarity_threshold=0.6)

        market_lookup = {m.ticker: m for m in markets}

        for ticker_a, ticker_b, similarity in pairs[:5]:  # Limit Claude calls
            market_a = market_lookup.get(ticker_a)
            market_b = market_lookup.get(ticker_b)
            if not market_a or not market_b:
                continue

            # Check cached relationship first
            cached = self._get_cached_relationship(
                ticker_a, ticker_b,
                price_a=market_a.yes_price, price_b=market_b.yes_price,
            )
            if cached is not None:
                if cached.get("arbitrage_exists"):
                    signal = self._build_subset_signal(
                        market_a, market_b, cached
                    )
                    if signal:
                        signals.append(signal)
                continue

            # Validate with Claude
            relationship = await self._validate_relationship(market_a, market_b)
            if relationship:
                relationship["cached_price_a"] = market_a.yes_price
                relationship["cached_price_b"] = market_b.yes_price
                self._cache_relationship(ticker_a, ticker_b, relationship)
                if relationship.get("arbitrage_exists"):
                    signal = self._build_subset_signal(
                        market_a, market_b, relationship
                    )
                    if signal:
                        signals.append(signal)

        return signals

    async def _validate_relationship(
        self, market_a: Market, market_b: Market
    ) -> Optional[dict]:
        """Use Claude to validate a logical relationship between two markets."""
        try:
            prompt = ARB_VALIDATION_TEMPLATE.format(
                question_a=market_a.question,
                price_a=market_a.yes_price,
                question_b=market_b.question,
                price_b=market_b.yes_price,
            )

            result = await self.forecaster.assess_market_with_prompt(
                market=market_a,
                custom_prompt=prompt,
            )

            if result and result.raw_response:
                import json
                try:
                    data = json.loads(result.raw_response)
                    return data
                except json.JSONDecodeError:
                    pass
        except Exception as e:
            logger.error(f"Arb validation failed: {e}", exc_info=True)

        return None

    def _build_subset_signal(
        self,
        market_a: Market,
        market_b: Market,
        relationship: dict,
    ) -> Optional[Signal]:
        """Build a signal from a validated subset/superset relationship."""
        rel_type = relationship.get("relationship", "")

        if rel_type == "subset_ab":
            # A is subset of B — if A resolves YES, B must also resolve YES.
            # Therefore B's YES price should be >= A's YES price.
            # If A > B, we buy YES on B (the underpriced superset).
            if market_a.yes_price > market_b.yes_price + self.min_edge:
                edge = market_a.yes_price - market_b.yes_price
                # Our estimate for B's true YES probability: at least as high as A's
                return Signal(
                    strategy=StrategyName.CROSS_ARB,
                    market_id=market_b.ticker,
                    market_question=market_b.question,
                    direction=Direction.BUY_YES,
                    edge=edge,
                    probability_estimate=min(0.99, market_b.yes_price + edge),
                    market_price=market_b.yes_price,
                    confidence=relationship.get("confidence", 0.5),
                    reasoning=f"Subset arb: {market_a.ticker}(YES={market_a.yes_price:.2f}) ⊂ {market_b.ticker}(YES={market_b.yes_price:.2f})",
                )
        elif rel_type == "subset_ba":
            # B is subset of A — B YES → A YES must hold
            if market_b.yes_price > market_a.yes_price + self.min_edge:
                edge = market_b.yes_price - market_a.yes_price
                return Signal(
                    strategy=StrategyName.CROSS_ARB,
                    market_id=market_a.ticker,
                    market_question=market_a.question,
                    direction=Direction.BUY_YES,
                    edge=edge,
                    probability_estimate=min(0.99, market_a.yes_price + edge),
                    market_price=market_a.yes_price,
                    confidence=relationship.get("confidence", 0.5),
                    reasoning=f"Subset arb: {market_b.ticker}(YES={market_b.yes_price:.2f}) ⊂ {market_a.ticker}(YES={market_a.yes_price:.2f})",
                )

        return None

    def _get_cached_relationship(
        self, ticker_a: str, ticker_b: str,
        price_a: float = 0.0, price_b: float = 0.0,
    ) -> Optional[dict]:
        """Check for a cached arb relationship in the database.

        Returns None if no cache exists, if the cache is older than 30 minutes,
        or if either market's price has moved >10% since the cache was created.
        """
        conn = self.db._get_conn()
        try:
            row = conn.execute(
                "SELECT relationship_data, validated_at FROM arb_relationships "
                "WHERE (market_a=? AND market_b=?) OR (market_a=? AND market_b=?)",
                (ticker_a, ticker_b, ticker_b, ticker_a),
            ).fetchone()
            if row:
                # TTL: invalidate cache older than 30 minutes
                validated_at = row["validated_at"]
                if validated_at:
                    from datetime import datetime, timedelta, timezone
                    try:
                        cached_time = datetime.fromisoformat(validated_at)
                        if datetime.now(timezone.utc) - cached_time > timedelta(minutes=30):
                            logger.debug(f"Arb cache expired for {ticker_a}/{ticker_b}")
                            return None
                    except (ValueError, TypeError):
                        return None  # Invalid timestamp — treat as expired
                import json
                data = json.loads(row["relationship_data"])

                # Price-based invalidation: if either market moved >25% since
                # cache time, the relationship may have changed materially.
                cached_price_a = data.get("cached_price_a", 0)
                cached_price_b = data.get("cached_price_b", 0)
                if cached_price_a > 0 and abs(price_a - cached_price_a) > 0.25:
                    logger.debug(
                        f"Arb cache price-invalidated for {ticker_a}: "
                        f"{cached_price_a:.2f} → {price_a:.2f}"
                    )
                    return None
                if cached_price_b > 0 and abs(price_b - cached_price_b) > 0.25:
                    logger.debug(
                        f"Arb cache price-invalidated for {ticker_b}: "
                        f"{cached_price_b:.2f} → {price_b:.2f}"
                    )
                    return None

                return data
            return None
        except Exception as e:
            logger.debug(f"Arb cache lookup failed for {ticker_a}/{ticker_b}: {e}")
            return None

    def _cache_relationship(self, ticker_a: str, ticker_b: str, data: dict):
        """Cache an arb relationship in the database."""
        import json
        conn = self.db._get_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO arb_relationships "
                "(market_a, market_b, relationship_data, validated_at) "
                "VALUES (?, ?, ?, ?)",
                (ticker_a, ticker_b, json.dumps(data), datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning(f"Failed to cache arb relationship: {e}")
