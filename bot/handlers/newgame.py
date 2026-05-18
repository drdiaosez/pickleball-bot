"""Parsing utilities for game creation.

The interactive /newgame conversation that used to live here has been
replaced by the newgame Mini App (see bot/static/newgame.html and the
/api/chat/{enc}/newgame endpoints in bot/http_server.py). This module
now exists only to host the two parsers — parse_datetime and
parse_payment — that other handlers (handlers/roster.py edit flows)
still import.

Keep this file minimal. New game-creation logic belongs in the HTTP
endpoint or in the Mini App; new chat flows that need natural-language
date parsing can import parse_datetime from here.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


# ─────────────────────── date parsing ─────────────────────── #

DAY_NAMES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


def parse_datetime(text: str, tz: ZoneInfo) -> datetime | None:
    """Parse a forgiving natural-language datetime.

    Accepts things like:
      'wed 6:30pm', 'thursday 7pm', 'tomorrow 6pm', 'today 5:30pm',
      '5/14 6:30pm', '05/20 9:30am', '2026-05-14 18:30'

    Strategy: extract the *date* first (it has stricter syntax — slashes,
    named days, 'today'/'tomorrow'), strip it out, then parse what's left
    as the time. Parsing time-first would mis-grab '05' from '05/20' as
    hour 5.

    Returns timezone-aware datetime in `tz`, or None on failure.
    """
    s = text.strip().lower()
    now = datetime.now(tz)

    # Strict ISO first
    try:
        dt = datetime.fromisoformat(text.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt
    except ValueError:
        pass

    target_date: "date | None" = None
    remainder = s  # what's left after the date is stripped

    # 1. Numeric date like 5/14, 05/20, 5-14, 5/14/2026 — match this FIRST
    #    because it has the most distinctive syntax (slash or dash between digits).
    num_date = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", s)
    if num_date:
        month = int(num_date.group(1))
        day = int(num_date.group(2))
        year = int(num_date.group(3)) if num_date.group(3) else now.year
        if year < 100:
            year += 2000
        try:
            target_date = datetime(year, month, day).date()
        except ValueError:
            return None
        # If no year was given and the date landed in the *distant* past,
        # they probably meant next year (e.g. typing "1/5" in late December).
        # But if it's a recent past date (within 60 days), they almost
        # certainly mistyped or meant a past game — DON'T roll forward to
        # next year. Returning None forces a re-prompt instead of silently
        # creating a game 11 months out.
        if num_date.group(3) is None and target_date < now.date():
            days_behind = (now.date() - target_date).days
            if days_behind > 60:
                # User typed something far in the past (e.g. "1/5" in Dec)
                target_date = datetime(year + 1, month, day).date()
            else:
                # Recent past — refuse the parse, let the user retry
                return None
        remainder = (s[:num_date.start()] + " " + s[num_date.end():]).strip()

    # 2. 'today' / 'tomorrow'
    elif "tomorrow" in s or "tmrw" in s:
        target_date = (now + timedelta(days=1)).date()
        remainder = re.sub(r"\b(tomorrow|tmrw)\b", " ", s).strip()
    elif "today" in s:
        target_date = now.date()
        remainder = re.sub(r"\btoday\b", " ", s).strip()

    # 3. Day of week (mon, tuesday, etc.)
    else:
        for name, weekday in DAY_NAMES.items():
            if re.search(rf"\b{name}\b", s):
                days_ahead = (weekday - now.weekday()) % 7
                if days_ahead == 0:
                    # Same weekday — we'll decide whether to bump to next week
                    # *after* we know the time. Mark as today for now.
                    pass
                target_date = (now + timedelta(days=days_ahead)).date()
                remainder = re.sub(rf"\b{name}\b", " ", s).strip()
                break

    if target_date is None:
        return None

    # Now parse the time from what's left. Accept "6:30pm", "6pm", "18:30",
    # "6:30", "7". Bare numbers without colon or meridiem are still allowed
    # (default AM per group preference).
    time_match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", remainder)
    if not time_match:
        return None
    hour = int(time_match.group(1))
    minute = int(time_match.group(2)) if time_match.group(2) else 0
    meridiem = time_match.group(3)

    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    # No meridiem? Leave as-is — this group plays mornings, so a bare
    # "6:30" or "7" means AM. PM games need an explicit "pm".

    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None

    # If the date was a same-weekday match and the resulting time has
    # already passed today, push to next week.
    today_weekday = now.weekday()
    if target_date == now.date() and target_date.weekday() == today_weekday:
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # Only push forward if this looked like a "wed" / "tomorrow"-style request,
        # not a numeric date that happens to be today.
        if candidate <= now and not num_date and "today" not in s:
            target_date = (now + timedelta(days=7)).date()

    dt = datetime.combine(target_date, datetime.min.time()).replace(
        hour=hour, minute=minute, tzinfo=tz
    )
    return dt


# ─────────────────────── money parsing ─────────────────────── #

# Accept "$5", "5", "5.50", "$5.50", "5,00". Reject anything else as bad input.
_PAYMENT_RE = re.compile(r"^\s*\$?\s*(\d+)(?:[.,](\d{1,2}))?\s*$")


def parse_payment(text: str) -> int | None:
    """Parse a money string into cents, or return None for unparseable input.

    Returns 0 for explicit zero ("0", "$0", "0.00") so the caller can
    distinguish that case from a parse failure if it cares. The edit
    flow in handlers/roster.py treats 0 as an explicit "clear the
    amount" signal.
    """
    m = _PAYMENT_RE.match(text)
    if not m:
        return None
    dollars = int(m.group(1))
    cents_str = m.group(2) or "0"
    # Pad single-digit cents: "5.5" → 50 cents, not 5 cents
    if len(cents_str) == 1:
        cents_str += "0"
    cents = int(cents_str)
    return dollars * 100 + cents
