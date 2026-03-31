"""Event Calendar Integration — scheduled catalysts for prediction markets.

Provides awareness of scheduled events (FOMC meetings, earnings dates,
legislative votes) that serve as catalysts for prediction markets. This
information is injected into Claude prompts and used for timing-based
edge adjustments.

Improves capital efficiency (don't enter too early) and catches
"catalyst has passed but market hasn't repriced" opportunities.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.core.models import Market, MarketCategory

logger = logging.getLogger(__name__)


@dataclass
class ScheduledEvent:
    """A scheduled event that may catalyze market movements."""

    name: str
    date: datetime
    category: MarketCategory
    impact: str  # "high", "medium", "low"
    description: str = ""

    @property
    def days_away(self) -> float:
        """Days until the event (negative if past)."""
        now = datetime.now(timezone.utc)
        return (self.date - now).total_seconds() / 86400


# ─── 2026 FOMC Meeting Schedule ───
# Source: Federal Reserve publishes these annually.
# Dates below are the announcement dates (second day of two-day meetings).
FOMC_2026_DATES = [
    datetime(2026, 1, 28, 19, 0, tzinfo=timezone.utc),   # Jan 27-28
    datetime(2026, 3, 18, 18, 0, tzinfo=timezone.utc),   # Mar 17-18
    datetime(2026, 5, 6, 18, 0, tzinfo=timezone.utc),    # May 5-6
    datetime(2026, 6, 17, 18, 0, tzinfo=timezone.utc),   # Jun 16-17
    datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc),   # Jul 28-29
    datetime(2026, 9, 16, 18, 0, tzinfo=timezone.utc),   # Sep 15-16
    datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc),   # Nov 3-4
    datetime(2026, 12, 16, 19, 0, tzinfo=timezone.utc),  # Dec 15-16
]

# Key economic data release patterns (approximate — actual dates vary)
# These are recurring monthly events that affect Fed/Macro markets
RECURRING_ECONOMIC_EVENTS = [
    ("CPI Release", "Usually 2nd week of month", "high"),
    ("PCE Release", "Usually last week of month", "high"),
    ("Jobs Report (NFP)", "Usually 1st Friday of month", "high"),
    ("FOMC Minutes", "Usually 3 weeks after meeting", "medium"),
]

# 2026 key political dates (US)
POLITICAL_2026_DATES = [
    ScheduledEvent(
        name="US Midterm Primary Season Begins",
        date=datetime(2026, 3, 3, 0, 0, tzinfo=timezone.utc),
        category=MarketCategory.POLITICS,
        impact="medium",
        description="State primaries begin — affects nomination markets",
    ),
    ScheduledEvent(
        name="US Midterm Elections",
        date=datetime(2026, 11, 3, 0, 0, tzinfo=timezone.utc),
        category=MarketCategory.POLITICS,
        impact="high",
        description="House and Senate midterm elections",
    ),
]


class EventCalendar:
    """Provides scheduled event awareness for prediction market analysis."""

    def __init__(self):
        self._fomc_events = [
            ScheduledEvent(
                name=f"FOMC Meeting {i + 1}/8",
                date=d,
                category=MarketCategory.FED_MACRO,
                impact="high",
                description="Federal Reserve interest rate decision",
            )
            for i, d in enumerate(FOMC_2026_DATES)
        ]
        self._political_events = list(POLITICAL_2026_DATES)
        self._all_events = self._fomc_events + self._political_events

    def get_upcoming_events(
        self,
        category: Optional[MarketCategory] = None,
        days_ahead: int = 30,
    ) -> list[ScheduledEvent]:
        """Get upcoming events within the specified window.

        Args:
            category: Filter by category, or None for all
            days_ahead: How far ahead to look (days)

        Returns:
            List of upcoming events, sorted by date
        """
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(days=days_ahead)

        events = []
        for event in self._all_events:
            if event.date < now:
                continue
            if event.date > cutoff:
                continue
            if category is not None and event.category != category:
                continue
            events.append(event)

        events.sort(key=lambda e: e.date)
        return events

    def get_context_string(self, market: Market) -> str:
        """Get formatted event context for Claude prompt injection.

        Args:
            market: The market being assessed

        Returns:
            Formatted string for prompt context, or empty string if no relevant events
        """
        # Determine relevant category
        category = market.category

        # Get upcoming events for this category (and high-impact from all categories)
        category_events = self.get_upcoming_events(category=category, days_ahead=30)
        high_impact_events = [
            e for e in self.get_upcoming_events(days_ahead=14)
            if e.impact == "high" and e.category != category
        ]

        all_relevant = category_events + high_impact_events
        if not all_relevant:
            return ""

        # Deduplicate by name
        seen_names = set()
        unique_events = []
        for e in all_relevant:
            if e.name not in seen_names:
                seen_names.add(e.name)
                unique_events.append(e)

        lines = ["UPCOMING CATALYSTS:"]
        for event in unique_events[:5]:  # Cap at 5 events
            days = event.days_away
            if days < 1:
                timing = "TODAY"
            elif days < 2:
                timing = "TOMORROW"
            else:
                timing = f"in {days:.0f} days ({event.date.strftime('%Y-%m-%d')})"

            lines.append(f"- {event.name}: {timing} [{event.impact} impact]")
            if event.description:
                lines.append(f"  {event.description}")

        return "\n".join(lines)

    def days_until_next_catalyst(
        self,
        market: Market,
    ) -> Optional[float]:
        """Get days until the next relevant catalyst for this market.

        Returns None if no relevant events are scheduled.
        """
        events = self.get_upcoming_events(
            category=market.category,
            days_ahead=90,
        )
        if not events:
            return None

        return events[0].days_away

    def get_next_fomc(self) -> Optional[ScheduledEvent]:
        """Get the next upcoming FOMC meeting."""
        now = datetime.now(timezone.utc)
        for event in self._fomc_events:
            if event.date > now:
                return event
        return None
