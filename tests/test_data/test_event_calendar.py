"""Tests for the Event Calendar Integration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.core.models import Market, MarketCategory, MarketToken
from src.data.event_calendar import (
    EventCalendar,
    FOMC_2026_DATES,
    ScheduledEvent,
)


def _make_market(category: MarketCategory = MarketCategory.FED_MACRO) -> Market:
    return Market(
        ticker="TEST-MKT",
        question="Will the Fed cut rates at the May 2026 meeting?",
        description="Resolves YES if FOMC announces rate cut.",
        category=category,
        tokens=[
            MarketToken(token_id="TEST_yes", outcome="Yes", price=0.45),
            MarketToken(token_id="TEST_no", outcome="No", price=0.55),
        ],
    )


class TestFOMCDates:
    def test_eight_fomc_meetings_in_2026(self):
        assert len(FOMC_2026_DATES) == 8

    def test_fomc_dates_are_in_2026(self):
        for d in FOMC_2026_DATES:
            assert d.year == 2026

    def test_fomc_dates_are_chronological(self):
        for i in range(1, len(FOMC_2026_DATES)):
            assert FOMC_2026_DATES[i] > FOMC_2026_DATES[i - 1]

    def test_fomc_dates_are_timezone_aware(self):
        for d in FOMC_2026_DATES:
            assert d.tzinfo is not None


class TestEventCalendar:
    def test_init(self):
        cal = EventCalendar()
        assert len(cal._fomc_events) == 8
        assert len(cal._all_events) > 8  # FOMC + political events

    def test_get_upcoming_events_all(self):
        cal = EventCalendar()
        events = cal.get_upcoming_events(days_ahead=365)
        # Should include at least some events (depends on current date)
        assert isinstance(events, list)

    def test_get_upcoming_events_filter_by_category(self):
        cal = EventCalendar()
        fed_events = cal.get_upcoming_events(
            category=MarketCategory.FED_MACRO, days_ahead=365,
        )
        for event in fed_events:
            assert event.category == MarketCategory.FED_MACRO

    def test_get_upcoming_events_sorted_by_date(self):
        cal = EventCalendar()
        events = cal.get_upcoming_events(days_ahead=365)
        for i in range(1, len(events)):
            assert events[i].date >= events[i - 1].date

    def test_get_upcoming_events_respects_days_ahead(self):
        cal = EventCalendar()
        short_window = cal.get_upcoming_events(days_ahead=1)
        long_window = cal.get_upcoming_events(days_ahead=365)
        assert len(short_window) <= len(long_window)


class TestContextString:
    def test_fed_market_gets_fomc_context(self):
        cal = EventCalendar()
        market = _make_market(MarketCategory.FED_MACRO)
        context = cal.get_context_string(market)
        # May or may not have content depending on current date vs 2026 dates
        assert isinstance(context, str)

    def test_politics_market_gets_political_context(self):
        cal = EventCalendar()
        market = _make_market(MarketCategory.POLITICS)
        context = cal.get_context_string(market)
        assert isinstance(context, str)

    def test_context_has_catalyst_header(self):
        """If any events are upcoming, the context should have the header."""
        cal = EventCalendar()
        # Inject a near-future event for testing
        cal._all_events.append(ScheduledEvent(
            name="Test Event",
            date=datetime.now(timezone.utc) + timedelta(days=3),
            category=MarketCategory.FED_MACRO,
            impact="high",
            description="Test description",
        ))
        market = _make_market(MarketCategory.FED_MACRO)
        context = cal.get_context_string(market)
        assert "UPCOMING CATALYSTS:" in context
        assert "Test Event" in context
        assert "3 days" in context

    def test_context_caps_at_5_events(self):
        cal = EventCalendar()
        # Inject many events
        for i in range(10):
            cal._all_events.append(ScheduledEvent(
                name=f"Event {i}",
                date=datetime.now(timezone.utc) + timedelta(days=i + 1),
                category=MarketCategory.FED_MACRO,
                impact="medium",
            ))
        market = _make_market(MarketCategory.FED_MACRO)
        context = cal.get_context_string(market)
        # Count event lines (lines starting with "- ")
        event_lines = [line for line in context.split("\n") if line.startswith("- ")]
        assert len(event_lines) <= 5


class TestDaysUntilCatalyst:
    def test_returns_none_when_no_events(self):
        cal = EventCalendar()
        # Use a category with no events
        market = _make_market(MarketCategory.CRYPTO)
        result = cal.days_until_next_catalyst(market)
        # Crypto has no scheduled events
        assert result is None

    def test_returns_days_for_fed_market(self):
        cal = EventCalendar()
        # Inject a future event
        cal._all_events.append(ScheduledEvent(
            name="Test FOMC",
            date=datetime.now(timezone.utc) + timedelta(days=10),
            category=MarketCategory.FED_MACRO,
            impact="high",
        ))
        market = _make_market(MarketCategory.FED_MACRO)
        result = cal.days_until_next_catalyst(market)
        assert result is not None
        assert 9.0 < result < 11.0


class TestScheduledEvent:
    def test_days_away_future(self):
        event = ScheduledEvent(
            name="Future Event",
            date=datetime.now(timezone.utc) + timedelta(days=5),
            category=MarketCategory.FED_MACRO,
            impact="high",
        )
        assert 4.9 < event.days_away < 5.1

    def test_days_away_past(self):
        event = ScheduledEvent(
            name="Past Event",
            date=datetime.now(timezone.utc) - timedelta(days=3),
            category=MarketCategory.FED_MACRO,
            impact="high",
        )
        assert event.days_away < 0


class TestGetNextFOMC:
    def test_returns_event_or_none(self):
        cal = EventCalendar()
        result = cal.get_next_fomc()
        # Depends on current date — may or may not be None
        if result is not None:
            assert result.category == MarketCategory.FED_MACRO
            assert "FOMC" in result.name
