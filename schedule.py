"""schedule.py — calendar + scheduling logic for the post queue.

The queue entries themselves live in app.py (JSON file or MySQL via db.py). This
module owns the *time* side of a post: parsing what the UI sends, deciding when a
queued post is due, and proposing free slots from the posting times in
config.yaml.

Times are handled in the machine's local timezone — that is the timezone the
calendar grid shows and the one the user thinks in. They are stored as naive ISO
strings (``2026-08-27T18:30:00``) to match the ``created`` field the queue has
always used and the DATETIME columns in db.py.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta

# A post is "due" once its slot has passed. A little slack absorbs a slow tick
# without shifting the post to the next minute-boundary check.
DUE_SLACK_SECONDS = 30

DEFAULT_SLOTS = ["09:00", "13:00", "18:00"]
DEFAULT_DAYS = ["mon", "tue", "wed", "thu", "fri"]

_DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


class ScheduleError(Exception):
    """An unusable date/time from the UI or a CSV column."""


# -- Parsing -------------------------------------------------------------------

_FORMATS = (
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
    "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%m/%d/%Y %H:%M", "%m/%d/%Y",
)


def parse_when(value: str | datetime | None) -> datetime | None:
    """Accept what the browser's datetime-local input sends, plus the date
    formats spreadsheets export. Empty means 'not scheduled'."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, microsecond=0)
    text = str(value).strip().replace("Z", "")
    for fmt in _FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(microsecond=0)
        except ValueError:
            continue
    try:                                     # last resort: full ISO with offset
        return datetime.fromisoformat(text).replace(tzinfo=None, microsecond=0)
    except ValueError:
        raise ScheduleError(f"Could not read '{value}' as a date/time.")


def iso(dt: datetime | None) -> str:
    return dt.isoformat(timespec="seconds") if dt else ""


def is_due(entry: dict, now: datetime | None = None) -> bool:
    """True when a scheduled entry's slot has arrived and it hasn't run yet."""
    if (entry.get("status") or "") != "scheduled":
        return False
    when = parse_when(entry.get("scheduled_at"))
    if not when:
        return False
    now = now or datetime.now()
    return when <= now + timedelta(seconds=DUE_SLACK_SECONDS)


# -- Config-driven posting slots ------------------------------------------------

def slot_config(config: dict | None) -> dict:
    """The ``schedule:`` block from config.yaml, with defaults filled in."""
    block = ((config or {}).get("schedule") or {})
    return {
        "times": list(block.get("times") or DEFAULT_SLOTS),
        "days": [d.lower()[:3] for d in (block.get("days") or DEFAULT_DAYS)],
        "auto_publish": bool(block.get("auto_publish", True)),
        "tick_seconds": int(block.get("tick_seconds", 60)),
    }


def _slot_times(cfg: dict) -> list[time]:
    out = []
    for raw in cfg["times"]:
        try:
            hh, mm = str(raw).split(":")[:2]
            out.append(time(int(hh), int(mm)))
        except (ValueError, TypeError):
            continue
    return sorted(out) or [time(9, 0)]


def next_slots(count: int, *, config: dict | None = None,
               taken: list[str] | None = None,
               start: datetime | None = None,
               horizon_days: int = 60) -> list[datetime]:
    """Propose ``count`` free posting slots from the configured times/days.

    Slots already used by a scheduled post are skipped, so filling the calendar
    twice never stacks two posts on the same minute."""
    cfg = slot_config(config)
    times = _slot_times(cfg)
    allowed = {_DAY_INDEX[d] for d in cfg["days"] if d in _DAY_INDEX} or set(range(7))
    used = {iso(parse_when(t)) for t in (taken or []) if t}
    now = start or datetime.now()

    out: list[datetime] = []
    day = now.date()
    for _ in range(horizon_days):
        if day.weekday() in allowed:
            for t in times:
                when = datetime.combine(day, t)
                if when > now and iso(when) not in used:
                    out.append(when)
                    used.add(iso(when))
                    if len(out) >= count:
                        return out
        day += timedelta(days=1)
    return out


# -- Calendar assembly ----------------------------------------------------------

def month_bounds(year: int, month: int) -> tuple[date, date]:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def month_grid(year: int, month: int, entries: list[dict], *,
               first_weekday: int = 0) -> dict:
    """Group queue entries into the weeks of a month for the calendar view.

    Returns whole weeks (padded with the neighbouring months' days) so the UI can
    render a fixed 7-column grid without doing date maths of its own."""
    cal = calendar.Calendar(firstweekday=first_weekday)
    by_day: dict[str, list[dict]] = {}
    undated: list[dict] = []
    for e in entries:
        when = parse_when(e.get("scheduled_at") or e.get("published_at"))
        if when:
            by_day.setdefault(when.date().isoformat(), []).append(e)
        else:
            undated.append(e)
    for day in by_day.values():
        day.sort(key=lambda e: e.get("scheduled_at") or "")

    weeks = []
    for week in cal.monthdatescalendar(year, month):
        weeks.append([{
            "date": d.isoformat(),
            "day": d.day,
            "in_month": d.month == month,
            "today": d == date.today(),
            "posts": by_day.get(d.isoformat(), []),
        } for d in week])

    return {
        "year": year,
        "month": month,
        "label": f"{calendar.month_name[month]} {year}",
        "weeks": weeks,
        "undated": undated,
        "counts": {k: len(v) for k, v in by_day.items()},
    }


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = (year * 12 + (month - 1)) + delta
    return idx // 12, idx % 12 + 1
