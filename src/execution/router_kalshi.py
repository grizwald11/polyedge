"""Kalshi live order router -- handles live order submission to Kalshi API.

Extracted from order_router.py (M-11) to isolate Kalshi-specific execution
logic (balance pre-flight, order creation, polling, reconciliation).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from src.core.models import (
    OrderStatus,
    OrderType,
    Trade,
    dollars_to_cents,
    kalshi_maker_fee,
    kalshi_taker_fee,
)

if TYPE_CHECKING:
    from src.core.models import Order
    from src.execution.order_router import OrderResult, OrderRouter

logger = logging.getLogger(__name__)


async def live_fill(router: OrderRouter, order: Order) -> OrderResult:
    """Submit order to Kalshi API for live execution.

    Orchestrates: validation -> order creation -> polling -> fill recording.

    Args:
        router: The parent OrderRouter instance (for shared state, DB, settings).
        order: The order to execute on Kalshi.
    """
    from src.execution.order_router import OrderResult

    # --- Pre-flight validation ---
    rejection = _validate_market(router, order)
    if rejection is not None:
        return rejection

    if not router._live_gates_passed():
        order.status = OrderStatus.REJECTED
        order.rejection_reason = "Live trading gates not passed"
        router._log_order(order)
        return OrderResult(
            success=False, order=order,
            error="Live trading gates not passed"
        )

    gate3_result = await router._check_gate3(order)
    if gate3_result is not None:
        return gate3_result

    kalshi_side, yes_price, side_err = _resolve_side_and_price(order)
    if side_err is not None:
        router._log_order(order)
        return side_err

    if not (1 <= yes_price <= 99):
        order.status = OrderStatus.REJECTED
        order.rejection_reason = f"Price converts to {yes_price} cents -- outside Kalshi range [1, 99]"
        router._log_order(order)
        logger.error(
            f"Order {order.id} rejected: yes_price={yes_price} cents "
            f"(from order.price=${order.price:.4f}, side={kalshi_side})"
        )
        return OrderResult(success=False, order=order, error=order.rejection_reason)

    balance_rejection = await _balance_preflight(router, order)
    if balance_rejection is not None:
        return balance_rejection

    # --- Order creation and fill ---
    try:
        result = await _create_order(router, order, kalshi_side, yes_price)

        # _create_order returns an OrderResult on timeout failure
        if isinstance(result, OrderResult):
            return result

        if result is None:
            order.status = OrderStatus.REJECTED
            order.rejection_reason = "Kalshi API returned None"
            router._log_order(order)
            return OrderResult(
                success=False, order=order, error="Kalshi API returned None"
            )

        kalshi_order_id = await _ensure_order_id(router, order, result)
        if not kalshi_order_id:
            order.status = OrderStatus.REJECTED
            order.rejection_reason = "Kalshi API did not return order_id"
            router._log_order(order)
            logger.error(
                f"Kalshi order response missing order_id and recovery found no match: {result}"
            )
            return OrderResult(
                success=False, order=order,
                error="Kalshi API did not return order_id",
            )

        order.exchange_order_id = kalshi_order_id
        final_status = await _poll_order_status(router, kalshi_order_id, result)

        return await _handle_final_status(router, order, result, final_status)

    except Exception as e:
        order.status = OrderStatus.REJECTED
        order.rejection_reason = str(e)
        router._log_order(order)
        logger.exception(f"Live order failed: {e}")
        return OrderResult(success=False, order=order, error=str(e))


# ---------------------------------------------------------------------------
# Helpers: validation & order prep
# ---------------------------------------------------------------------------


def _validate_market(router: OrderRouter, order: Order) -> Optional[OrderResult]:
    """C-3: Reject orders on closed/settled/halted markets before hitting the API."""
    from src.execution.order_router import OrderResult

    market_row = router.db.get_market(order.market_id)
    if market_row is not None:
        mkt_closed = market_row.get("closed") or market_row.get("active") == 0
        mkt_status = str(market_row.get("status", "")).lower()
        if mkt_closed or mkt_status in ("closed", "settled", "halted", "determined"):
            order.status = OrderStatus.REJECTED
            order.rejection_reason = f"Market is {mkt_status or 'closed'} -- cannot trade"
            router._log_order(order)
            logger.warning(f"Order {order.id} rejected: {order.rejection_reason}")
            return OrderResult(success=False, order=order, error=order.rejection_reason)
    return None


def _resolve_side_and_price(order: Order) -> tuple[Optional[str], int, Optional[OrderResult]]:
    """Determine Kalshi side and yes_price in cents.

    Returns (kalshi_side, yes_price_cents, error_result).
    If error_result is not None the caller should return it immediately.
    """
    from src.execution.order_router import OrderResult

    kalshi_side = order.kalshi_side
    if kalshi_side is None:
        order.status = OrderStatus.REJECTED
        order.rejection_reason = "Missing kalshi_side -- order_builder must set this explicitly"
        logger.error(
            f"Order {order.id} missing kalshi_side -- rejected to prevent side mismatch. "
            f"Ensure order_builder sets kalshi_side from Direction enum."
        )
        return None, 0, OrderResult(success=False, order=order, error="Missing kalshi_side")

    kalshi_type = "limit" if order.order_type == OrderType.GTC else "market"
    if kalshi_side == "no":
        yes_price = dollars_to_cents(1.0 - order.price)
    else:
        yes_price = dollars_to_cents(order.price)

    return kalshi_side, yes_price, None


# ---------------------------------------------------------------------------
# Helpers: order creation & ID recovery
# ---------------------------------------------------------------------------


async def _create_order(
    router: OrderRouter, order: Order, kalshi_side: str, yes_price: int
) -> Optional[dict | OrderResult]:
    """Submit the order to Kalshi with timeout and reconciliation on timeout.

    Returns the API result dict on success, an OrderResult on timeout failure,
    or None if the API returned None.
    """
    from src.execution.order_router import OrderResult

    kalshi_type = "limit" if order.order_type == OrderType.GTC else "market"

    # C-1 FIX: Check per-market cooldown to prevent duplicate orders after timeout
    cooldown_key = f"{order.market_id}:{order.side.value}"
    if hasattr(router, "_timeout_cooldowns"):
        cooldown_until = router._timeout_cooldowns.get(cooldown_key)
        if cooldown_until is not None and datetime.now(timezone.utc) < cooldown_until:
            remaining = (cooldown_until - datetime.now(timezone.utc)).total_seconds()
            order.status = OrderStatus.REJECTED
            order.rejection_reason = (
                f"Market {order.market_id} under timeout cooldown "
                f"({remaining:.0f}s remaining) -- previous order may still be processing"
            )
            router._log_order(order)
            logger.warning(f"C-1: {order.rejection_reason}")
            return OrderResult(success=False, order=order, error=order.rejection_reason)

    try:
        result = await asyncio.wait_for(
            router.kalshi.create_order(
                ticker=order.market_id,
                side=kalshi_side,
                yes_price=yes_price,
                count=int(order.size),
                order_type=kalshi_type,
                action=order.side.value.lower(),
            ),
            timeout=25.0,  # C-1 FIX: Increased from 15s to 25s
        )
    except asyncio.TimeoutError:
        logger.error(
            f"Order creation timed out for {order.market_id} -- "
            f"order may exist on Kalshi. Checking open orders for reconciliation."
        )
        # C-1 FIX: Set 120s cooldown on this market/side to prevent duplicate orders
        if not hasattr(router, "_timeout_cooldowns"):
            router._timeout_cooldowns = {}
        from datetime import timedelta
        router._timeout_cooldowns[cooldown_key] = datetime.now(timezone.utc) + timedelta(seconds=120)

        result = await _reconcile_after_timeout(router, order)
        if result is None:
            order.status = OrderStatus.PENDING_REVIEW
            order.rejection_reason = "Timeout creating order -- may exist on Kalshi (unconfirmed)"
            router._log_order(order)
            logger.critical(
                f"ORPHANED ORDER RISK: order for {order.market_id} timed out and reconciliation "
                f"found no match after 3 attempts. Order may still be processing on Kalshi. "
                f"Manual review required -- check Kalshi dashboard. "
                f"120s cooldown set on {cooldown_key}."
            )
            return OrderResult(success=False, order=order, error="Timeout -- order status unknown, manual review required")
    return result


async def _ensure_order_id(router: OrderRouter, order: Order, result: dict) -> str:
    """Extract or recover the Kalshi order_id from the API response."""
    kalshi_order_id = (result.get("order_id") or "").strip()
    if not kalshi_order_id:
        kalshi_order_id = await _recover_order_id(router, order, result)

    if "status" not in result:
        logger.warning(
            f"Kalshi order response missing 'status' field: {result}"
        )

    return kalshi_order_id


# ---------------------------------------------------------------------------
# Helpers: status handling & fill recording
# ---------------------------------------------------------------------------


async def _handle_final_status(
    router: OrderRouter, order: Order, result: dict, final_status: str
) -> OrderResult:
    """Map the final poll status to an OrderResult, recording fills as needed."""
    from src.execution.order_router import OrderResult

    now = datetime.now(timezone.utc)

    if final_status in ("executed",):
        order.status = OrderStatus.FILLED
        order.filled_at = now
        _apply_fill_price(order, result)
        return await _record_fill(router, order, now)

    if final_status == "resting":
        order.status = OrderStatus.OPEN
        # H-12: Pending cost already reserved in route_order before submission
        router._log_order(order)
        logger.info(
            f"[LIVE] Order resting: {order.side.value} {int(order.size)}x "
            f"{order.token_id} @ ${order.price:.2f} "
            f"(pending_cost=${router._pending_order_cost:.2f})"
        )
        return OrderResult(success=True, order=order, trade=None)

    if final_status in ("canceled", "cancelled"):
        order.status = OrderStatus.CANCELLED
        order.cancelled_at = now
        await router._remove_pending(order.id)
        router._log_order(order)
        return OrderResult(
            success=False, order=order, error="Order was cancelled"
        )

    # Fallback: treat unrecognised status as filled (preserves original behaviour
    # where code fell through the if/elif chain to the trade-record block).
    order.status = OrderStatus.FILLED
    order.filled_at = now
    _apply_fill_price(order, result)
    return await _record_fill(router, order, now)


def _apply_fill_price(order: Order, result: dict) -> None:
    """Set order.fill_price from API avg_price or fall back to order.price."""
    api_fill_price = result.get("avg_price")
    if api_fill_price is not None:
        try:
            api_fill_price = float(api_fill_price)
            if not (0 < api_fill_price <= 100):
                raise ValueError(f"avg_price out of range: {api_fill_price}")
            order.fill_price = api_fill_price / 100.0
        except (TypeError, ValueError) as e:
            logger.error(f"Invalid avg_price from Kalshi: {result.get('avg_price')!r} -- using order price")
            order.fill_price = order.price
    else:
        order.fill_price = order.price


async def _record_fill(router: OrderRouter, order: Order, now: datetime) -> OrderResult:
    """Create a Trade record for a filled order, log it, and return the OrderResult."""
    from src.execution.order_router import OrderResult

    fill_price = order.fill_price if order.fill_price is not None else order.price
    price_cents = dollars_to_cents(fill_price)
    if order.order_type == OrderType.GTC:
        fee_cents = kalshi_maker_fee(int(order.size), price_cents)
    else:
        fee_cents = kalshi_taker_fee(int(order.size), price_cents)

    trade = Trade(
        order_id=order.id,
        market_id=order.market_id,
        platform=order.platform,
        token_id=order.token_id,
        side=order.side,
        price=fill_price,
        size=order.size,
        fee=fee_cents / 100.0,
        realized_pnl=0.0,
        strategy=order.strategy,
        paper=False,
        timestamp=now,
    )

    await router._remove_pending(order.id)
    router._log_order(order)
    router.db.log_trade(trade)

    slippage = abs(fill_price - order.price)
    if slippage > 0.01:
        logger.warning(
            f"[LIVE] Slippage alert: expected ${order.price:.2f}, "
            f"filled ${fill_price:.2f} (slippage=${slippage:.3f})"
        )

    logger.info(
        f"[LIVE] Filled: {order.side.value} {int(order.size)}x "
        f"{order.token_id} @ ${fill_price:.2f}"
    )

    return OrderResult(success=True, order=order, trade=trade)


# ---------------------------------------------------------------------------
# Helpers: balance, reconciliation, recovery, polling (unchanged)
# ---------------------------------------------------------------------------


async def _balance_preflight(router: OrderRouter, order: Order) -> Optional[OrderResult]:
    """M-14: Balance pre-flight check. Returns OrderResult rejection if fails, None if OK.

    H-1 FIX: Subtracts pending_order_cost from the exchange balance so that
    capital reserved for resting (unfilled) orders is not double-committed.
    """
    from src.execution.order_router import OrderResult

    large_order_threshold = router.settings.trading.bankroll * 0.10
    is_large_order = order.cost > large_order_threshold
    balance_retries = 3 if is_large_order else 1
    balance_checked = False
    for _bal_attempt in range(balance_retries):
        try:
            raw_balance = await asyncio.wait_for(router.kalshi.get_balance(), timeout=5.0)
            if raw_balance is None:
                # Balance unavailable — skip preflight (will be caught by exchange)
                balance_checked = False
                break
            # Keep Decimal precision through the entire comparison path (C-1).
            # raw_balance is Decimal from get_balance(); convert pending and
            # order cost to Decimal for cent-accurate arithmetic.
            from decimal import Decimal
            balance_dec = raw_balance  # Already Decimal from get_balance()
            pending_dec = Decimal(str(router.pending_order_cost))
            cost_dec = Decimal(str(order.cost))
            available_dec = balance_dec - pending_dec
            logger.debug(
                "Balance preflight: exchange=$%.2f - pending=$%.2f = available=$%.2f, order=$%.2f",
                float(balance_dec), float(pending_dec), float(available_dec), float(cost_dec),
            )
            if cost_dec > available_dec:
                order.status = OrderStatus.REJECTED
                order.rejection_reason = (
                    f"Insufficient balance: order cost ${float(cost_dec):.2f} > "
                    f"available ${float(available_dec):.2f} "
                    f"(exchange=${float(balance_dec):.2f} - pending=${float(pending_dec):.2f})"
                )
                router._log_order(order)
                logger.warning(f"Balance pre-flight failed: {order.rejection_reason}")
                return OrderResult(success=False, order=order, error=order.rejection_reason)
            balance_checked = True
            break
        except (asyncio.TimeoutError, Exception) as e:
            if is_large_order and _bal_attempt < balance_retries - 1:
                logger.warning(
                    f"Balance pre-flight retry {_bal_attempt + 1}/{balance_retries} "
                    f"for large order (${order.cost:.2f}): {e}"
                )
                await asyncio.sleep(1.0 * (2 ** _bal_attempt))
            else:
                logger.debug(f"Balance pre-flight check skipped: {e}")
    if is_large_order and not balance_checked:
        order.status = OrderStatus.REJECTED
        order.rejection_reason = (
            f"Balance pre-flight failed after {balance_retries} retries -- "
            f"blocking large order (${order.cost:.2f} > 10% bankroll)"
        )
        router._log_order(order)
        logger.error(f"H-1: {order.rejection_reason}")
        return OrderResult(success=False, order=order, error=order.rejection_reason)
    return None


async def _reconcile_after_timeout(router: OrderRouter, order: Order) -> Optional[dict]:
    """After a create_order timeout, check Kalshi for matching recent orders.

    C-1 FIX: Retries up to 3 times with 5s waits between attempts.
    Returns the matching order dict if found, None otherwise.
    """
    max_reconcile_attempts = 3
    reconcile_delay = 5.0

    for attempt in range(max_reconcile_attempts):
        if attempt > 0:
            logger.info(
                f"Reconciliation attempt {attempt + 1}/{max_reconcile_attempts} "
                f"for {order.market_id} (waiting {reconcile_delay}s for order to propagate)"
            )
            await asyncio.sleep(reconcile_delay)

        try:
            open_orders = await asyncio.wait_for(
                router.kalshi.get_open_orders(),
                timeout=10.0,
            )
            for oo in open_orders:
                price_match = abs(oo.get("yes_price", 0) / 100 - order.price) < 0.01
                side_match = oo.get("side", "").lower() == (order.kalshi_side or "").lower()
                if (
                    oo.get("ticker") == order.market_id
                    and oo.get("count") == int(order.size)
                    and price_match
                    and side_match
                ):
                    logger.warning(
                        f"Reconciliation found matching order on Kalshi "
                        f"(attempt {attempt + 1}): {oo.get('order_id')}"
                    )
                    return oo
        except asyncio.TimeoutError:
            logger.warning(
                f"Reconciliation attempt {attempt + 1}/{max_reconcile_attempts} "
                f"timed out for {order.market_id}"
            )
        except Exception as e:
            logger.error(
                f"Reconciliation attempt {attempt + 1}/{max_reconcile_attempts} "
                f"failed: {e}", exc_info=True
            )

    logger.error(
        f"All {max_reconcile_attempts} reconciliation attempts failed "
        f"for {order.market_id} -- order status unknown"
    )
    return None


async def _recover_order_id(router: OrderRouter, order: Order, result: dict) -> str:
    """Attempt to recover a missing order_id from Kalshi's open orders list.

    Returns the recovered order_id string, or empty string if recovery fails.
    """
    logger.warning(
        f"Kalshi order response missing order_id for {order.market_id} -- "
        f"attempting recovery via get_open_orders()"
    )
    kalshi_order_id = ""
    try:
        open_orders = await asyncio.wait_for(
            router.kalshi.get_open_orders(),
            timeout=10.0,
        )
        # C-1 FIX: Add timestamp-based matching to avoid recovering
        # the wrong order when multiple identical orders exist at
        # the same price. Orders created within 30s of our attempt
        # are candidates; prefer the most recent.
        candidates = []
        for oo in open_orders:
            # H-3: Compare in integer cents to avoid float tolerance issues
            price_match = abs(int(oo.get("yes_price", 0)) - dollars_to_cents(order.price)) <= 1
            side_match = oo.get("side", "").lower() == (order.kalshi_side or "").lower()
            count_match = oo.get("count", 0) == int(order.size)
            if (
                oo.get("ticker") == order.market_id
                and price_match
                and side_match
                and count_match
            ):
                recovered_id = (oo.get("order_id") or "").strip()
                if recovered_id:
                    # M-2: Score by timestamp proximity AND field matching
                    # to improve accuracy when multiple similar orders exist.
                    oo_created = oo.get("created_time") or oo.get("created_at") or ""
                    time_score = 0
                    if oo_created and hasattr(order, "created_at") and order.created_at:
                        try:
                            oo_dt = datetime.fromisoformat(oo_created.replace("Z", "+00:00"))
                            time_diff = abs((oo_dt - order.created_at).total_seconds())
                            if time_diff <= 30:  # Only match orders within 30s
                                time_score = 30 - time_diff  # Higher = more recent
                            else:
                                continue  # Too old to be our order
                        except (ValueError, TypeError):
                            time_score = 0  # Can't parse, use as fallback

                    # M-2: Additional field-matching score (0-30 points)
                    field_score = 0
                    if oo.get("ticker") == order.market_id:
                        field_score += 10  # Ticker match
                    if oo.get("side", "").lower() == (order.kalshi_side or "").lower():
                        field_score += 10  # Side match
                    oo_price_cents = int(oo.get("yes_price", 0))
                    order_price_cents = dollars_to_cents(order.price)
                    if abs(oo_price_cents - order_price_cents) <= 1:
                        field_score += 10  # Exact price match

                    combined_score = time_score + field_score
                    candidates.append((combined_score, recovered_id, oo))
        # Pick the best candidate (highest time_score = closest match)
        if candidates:
            candidates.sort(key=lambda c: c[0], reverse=True)
            _, kalshi_order_id, result_update = candidates[0]
            # Update the result dict in-place with recovered data
            result.update(result_update)
            logger.warning(
                f"Orphaned order recovery succeeded: matched order_id={kalshi_order_id} "
                f"for {order.market_id} via open orders list "
                f"({len(candidates)} candidate(s), best time_score={candidates[0][0]:.1f})"
            )
    except Exception as rec_err:
        logger.error(f"Orphaned order recovery failed: {rec_err}", exc_info=True)

    return kalshi_order_id


async def _poll_order_status(
    router: OrderRouter, kalshi_order_id: str, initial_data: dict
) -> str:
    """Poll Kalshi for order fill status up to configured max attempts.

    Returns the final status string.
    """
    status = initial_data.get("status", "").lower()
    if status in ("executed", "canceled", "cancelled", "expired", "rejected", "failed"):
        return status

    if not kalshi_order_id:
        return status

    max_attempts = router.settings.execution.max_poll_attempts
    poll_delay = router.settings.execution.order_poll_delay_seconds
    poll_timeout = router.settings.execution.order_poll_timeout_seconds
    for attempt in range(max_attempts):
        await asyncio.sleep(poll_delay)
        try:
            order_data = await asyncio.wait_for(
                router.kalshi.get_order(kalshi_order_id),
                timeout=poll_timeout,
            )
            if order_data:
                status = order_data.get("status", "").lower()
                if status in ("executed", "canceled", "cancelled", "expired", "rejected", "failed"):
                    return status
        except asyncio.TimeoutError:
            logger.warning(f"Order poll attempt {attempt + 1} timed out after {poll_timeout}s")
        except Exception as e:
            logger.warning(f"Order poll attempt {attempt + 1} failed: {e}")

    return status  # Return last known status
