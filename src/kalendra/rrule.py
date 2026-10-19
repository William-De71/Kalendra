"""RFC 5545 recurrence rule expansion (the useful subset).

This implementation only serves server-side time filtering (`<C:time-range>`)
and the computation of indexed bounds. It covers
FREQ=MINUTELY|HOURLY|DAILY|WEEKLY|MONTHLY|YEARLY with INTERVAL, COUNT, UNTIL,
BYMONTH, BYWEEKNO, BYYEARDAY, BYMONTHDAY, BYDAY (with ordinal position),
BYHOUR, BYMINUTE, BYSETPOS and WKST.

For an unsupported rule, an "open" expansion is preferred (the recurrence is
treated as infinite) over hiding events: a CalDAV client filters locally
anyway, whereas an event missing from the response is an event lost to the
user.
"""

from __future__ import annotations

import calendar as _calendar
from collections.abc import Iterator
from datetime import date, datetime, timedelta

WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

SUPPORTED_FREQ = {"MINUTELY", "HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY"}

#: Ranges RFC 5545 §3.3.10 allows; zero is never valid where negatives are.
RANGES = {
    "BYMONTH": (1, 12, False),
    "BYMONTHDAY": (1, 31, True),
    "BYYEARDAY": (1, 366, True),
    "BYWEEKNO": (1, 53, True),
    "BYHOUR": (0, 23, False),
    "BYMINUTE": (0, 59, False),
    "BYSETPOS": (1, 366, True),
}

#: Nombre maximal d'occurrences produites par appel (garde-fou anti-boucle).
MAX_OCCURRENCES = 10_000


class UnsupportedRule(ValueError):
    """The rule falls outside the supported subset; callers must stay lenient."""


def parse_rrule(value: str) -> dict[str, object]:
    """Decode an RRULE property value into a normalised dictionary."""
    rule: dict[str, object] = {}
    for chunk in value.replace(" ", "").split(";"):
        if not chunk or "=" not in chunk:
            continue
        key, _, raw = chunk.partition("=")
        key = key.upper()
        raw = raw.strip()
        if key in {"COUNT", "INTERVAL"}:
            try:
                rule[key] = int(raw)
            except ValueError:
                continue
        elif key in {
            "BYMONTH",
            "BYMONTHDAY",
            "BYHOUR",
            "BYMINUTE",
            "BYSETPOS",
            "BYYEARDAY",
            "BYWEEKNO",
        }:
            values = []
            for part in raw.split(","):
                try:
                    values.append(int(part))
                except ValueError:
                    continue
            rule[key] = values
        elif key == "BYDAY":
            rule[key] = [part.upper() for part in raw.split(",") if part]
        else:
            rule[key] = raw.upper() if key in {"FREQ", "WKST"} else raw
    return rule


def _parse_byday(token: str) -> tuple[int | None, int]:
    """`3MO` -> (3, 0) ; `-1SU` -> (-1, 6) ; `WE` -> (None, 2)."""
    token = token.strip().upper()
    day = token[-2:]
    if day not in WEEKDAYS:
        raise UnsupportedRule(f"BYDAY inconnu : {token}")
    prefix = token[:-2]
    if not prefix:
        return None, WEEKDAYS[day]
    try:
        return int(prefix), WEEKDAYS[day]
    except ValueError as exc:
        raise UnsupportedRule(f"BYDAY inconnu : {token}") from exc


def _month_days(year: int, month: int) -> int:
    return _calendar.monthrange(year, month)[1]


def _add_months(moment: datetime, months: int) -> datetime:
    total = moment.month - 1 + months
    year = moment.year + total // 12
    month = total % 12 + 1
    day = min(moment.day, _month_days(year, month))
    return moment.replace(year=year, month=month, day=day)


def _matches_filters(moment: datetime, rule: dict) -> bool:
    months = rule.get("BYMONTH")
    if months and moment.month not in months:
        return False
    monthdays = rule.get("BYMONTHDAY")
    if monthdays:
        last = _month_days(moment.year, moment.month)
        allowed = {d if d > 0 else last + d + 1 for d in monthdays}
        if moment.day not in allowed:
            return False
    bydays = rule.get("BYDAY")
    if bydays:
        weekdays = set()
        for token in bydays:
            _, weekday = _parse_byday(token)
            weekdays.add(weekday)
        if moment.weekday() not in weekdays:
            return False
    yeardays = rule.get("BYYEARDAY")
    if yeardays:
        length = 366 if _calendar.isleap(moment.year) else 365
        allowed = {d if d > 0 else length + d + 1 for d in yeardays}
        if moment.timetuple().tm_yday not in allowed:
            return False
    return True


def _week_one(year: int, wkst: int) -> date:
    """First day of week 1: the first week holding at least four days of `year`.

    January 4th always falls in that week, whatever day weeks start on, which
    is the whole trick (RFC 5545 §3.3.10, same rule as ISO 8601 for WKST=MO).
    """
    jan4 = date(year, 1, 4)
    return jan4 - timedelta(days=(jan4.weekday() - wkst) % 7)


def _yearly_days(year: int, rule: dict) -> list[date]:
    """Days of a YEARLY period when BYWEEKNO or BYYEARDAY drives the expansion.

    Applied in RFC 5545 order: BYWEEKNO and BYYEARDAY expand, then BYMONTH,
    BYMONTHDAY and BYDAY narrow. Week 1 may start in late December and the last
    week may end in January: those days belong to this year's period, which
    is why candidates are not clipped to the calendar year.
    """
    first = date(year, 1, 1)
    length = 366 if _calendar.isleap(year) else 365

    weeknos = rule.get("BYWEEKNO")
    if weeknos:
        wkst = WEEKDAYS.get(str(rule.get("WKST", "MO")), 0)
        start = _week_one(year, wkst)
        weeks = (_week_one(year + 1, wkst) - start).days // 7
        wanted = {n if n > 0 else weeks + n + 1 for n in weeknos}
        numbers = sorted(wanted & set(range(1, weeks + 1)))
        days = [start + timedelta(days=7 * (n - 1) + i) for n in numbers for i in range(7)]
    else:
        days = [first + timedelta(days=i) for i in range(length)]

    yeardays = rule.get("BYYEARDAY")
    if yeardays:
        allowed = {d if d > 0 else length + d + 1 for d in yeardays}
        days = [d for d in days if (d - first).days + 1 in allowed]

    months = rule.get("BYMONTH")
    if months:
        days = [d for d in days if d.month in months]

    monthdays = rule.get("BYMONTHDAY")
    if monthdays:

        def _allowed(d: date) -> bool:
            last = _month_days(d.year, d.month)
            return d.day in {m if m > 0 else last + m + 1 for m in monthdays}

        days = [d for d in days if _allowed(d)]

    bydays = rule.get("BYDAY")
    if bydays:
        parsed = [_parse_byday(token) for token in bydays]
        if any(ordinal is not None for ordinal, _ in parsed):
            # "the 2nd Monday" of a week number means nothing; of a set of
            # year days, too rare to be worth the code. Stay lenient.
            raise UnsupportedRule("BYDAY ordinal avec BYWEEKNO / BYYEARDAY")
        weekdays = {weekday for _, weekday in parsed}
        days = [d for d in days if d.weekday() in weekdays]

    return sorted(set(days))


def _apply_times(days: list[date], base: datetime, rule: dict) -> list[datetime]:
    hours = rule.get("BYHOUR") or [base.hour]
    minutes = rule.get("BYMINUTE") or [base.minute]
    result = []
    for day in days:
        for hour in sorted(hours):
            for minute in sorted(minutes):
                result.append(
                    datetime(
                        day.year,
                        day.month,
                        day.day,
                        hour % 24,
                        minute % 60,
                        base.second,
                        tzinfo=base.tzinfo,
                    )
                )
    return sorted(result)


def _bysetpos(items: list, positions: list[int]) -> list:
    picked = []
    for pos in positions:
        if pos > 0 and pos <= len(items):
            picked.append(items[pos - 1])
        elif pos < 0 and -pos <= len(items):
            picked.append(items[pos])
    return sorted(set(picked))


def _monthly_days(year: int, month: int, rule: dict, base: datetime) -> list[date]:
    last = _month_days(year, month)
    days: set[int] = set()

    monthdays = rule.get("BYMONTHDAY")
    bydays = rule.get("BYDAY")

    if monthdays:
        for value in monthdays:
            day = value if value > 0 else last + value + 1
            if 1 <= day <= last:
                days.add(day)
    if bydays:
        for token in bydays:
            ordinal, weekday = _parse_byday(token)
            matching = [d for d in range(1, last + 1) if date(year, month, d).weekday() == weekday]
            if ordinal is None:
                days.update(matching)
            elif ordinal > 0 and ordinal <= len(matching):
                days.add(matching[ordinal - 1])
            elif ordinal < 0 and -ordinal <= len(matching):
                days.add(matching[ordinal])
    if not monthdays and not bydays and base.day <= last:
        # RFC 5545 §3.3.10: an invalid date is skipped, not moved. A monthly
        # event on the 31st has no April instance, and a yearly one on
        # February 29th only recurs in leap years — clients compute it that
        # way, so the server must too.
        days.add(base.day)

    if monthdays and bydays:
        # Intersection: BYDAY narrows which days of the month are kept.
        weekdays = {_parse_byday(token)[1] for token in bydays}
        days = {d for d in days if date(year, month, d).weekday() in weekdays}

    return [date(year, month, d) for d in sorted(days)]


def iter_occurrences(
    dtstart: datetime, rule: dict, *, limit: int = MAX_OCCURRENCES, horizon: datetime | None = None
) -> Iterator[datetime]:
    """Yield the occurrences of an RRULE from `dtstart` (inclusive).

    Stops at `limit` occurrences, at `UNTIL`/`COUNT`, at `horizon`, or where
    `datetime` stops: a long series can step past the year 9999, which raises
    OverflowError or ValueError deep in date arithmetic. That is the end of
    representable time, so the series simply ends there.
    """
    try:
        yield from _occurrences(dtstart, rule, limit=limit, horizon=horizon)
    except UnsupportedRule:
        raise
    except (OverflowError, ValueError):
        return


def _occurrences(
    dtstart: datetime, rule: dict, *, limit: int, horizon: datetime | None
) -> Iterator[datetime]:
    freq = str(rule.get("FREQ", "")).upper()
    if freq not in SUPPORTED_FREQ:
        raise UnsupportedRule(f"FREQ non gérée : {freq or '(absente)'}")
    # RFC 5545 confines BYWEEKNO to YEARLY and keeps BYYEARDAY away from
    # DAILY, WEEKLY and MONTHLY. Such a rule is malformed: treated as
    # unsupported, so the event is shown rather than guessed at.
    if rule.get("BYWEEKNO") and freq != "YEARLY":
        raise UnsupportedRule("BYWEEKNO hors FREQ=YEARLY")
    if rule.get("BYYEARDAY") and freq in {"DAILY", "WEEKLY", "MONTHLY"}:
        raise UnsupportedRule(f"BYYEARDAY avec FREQ={freq}")
    by_year = freq == "YEARLY" and bool(rule.get("BYYEARDAY") or rule.get("BYWEEKNO"))

    # Out-of-range parts (BYMONTH=13) would crash date arithmetic further
    # down; a malformed rule is unsupported, so the event is shown, not lost.
    for part, (low, high, signed) in RANGES.items():
        for value in rule.get(part) or ():
            magnitude = abs(value) if signed else value
            if not low <= magnitude <= high:
                raise UnsupportedRule(f"{part}={value} hors limites")

    interval = max(1, int(rule.get("INTERVAL", 1) or 1))
    count = rule.get("COUNT")
    count = int(count) if count else None
    until_raw = rule.get("UNTIL")
    until = _parse_until(str(until_raw), dtstart) if until_raw else None
    setpos = rule.get("BYSETPOS")

    emitted = 0
    guard = 0

    if freq in {"MINUTELY", "HOURLY", "DAILY"}:
        step = {
            "MINUTELY": timedelta(minutes=interval),
            "HOURLY": timedelta(hours=interval),
            "DAILY": timedelta(days=interval),
        }[freq]
        moment = dtstart
        while True:
            guard += 1
            if guard > limit * 40 + 1000:
                return
            if until and moment > until:
                return
            if horizon and moment > horizon:
                return
            if _matches_filters(moment, rule):
                yield moment
                emitted += 1
                if count and emitted >= count:
                    return
                if emitted >= limit:
                    return
            moment = moment + step

    if freq == "WEEKLY":
        wkst = WEEKDAYS.get(str(rule.get("WKST", "MO")), 0)
        bydays = rule.get("BYDAY") or []
        weekdays = sorted({_parse_byday(t)[1] for t in bydays}) or [dtstart.weekday()]
        offset = (dtstart.weekday() - wkst) % 7
        week_start = (dtstart - timedelta(days=offset)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        while True:
            guard += 1
            if guard > limit + 2000:
                return
            days = []
            for weekday in weekdays:
                delta = (weekday - wkst) % 7
                days.append((week_start + timedelta(days=delta)).date())
            moments = _apply_times(sorted(days), dtstart, rule)
            if setpos:
                moments = _bysetpos(moments, setpos)
            for moment in moments:
                if moment < dtstart:
                    continue
                if until and moment > until:
                    return
                if horizon and moment > horizon:
                    return
                if not _matches_filters(moment, {"BYMONTH": rule.get("BYMONTH")}):
                    continue
                yield moment
                emitted += 1
                if count and emitted >= count:
                    return
                if emitted >= limit:
                    return
            week_start = week_start + timedelta(weeks=interval)
            if horizon and week_start > horizon:
                return

    # MONTHLY et YEARLY
    # Only the cursor's year and month matter. Pinning it to the 1st keeps
    # February 29th from crashing `replace(year=…)` in a common year.
    cursor = dtstart.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while True:
        guard += 1
        if guard > limit + 2000:
            return
        if by_year:
            # One period per year: the day set already spans the whole year.
            periods = [(cursor.year, 0)]
        elif freq == "MONTHLY":
            periods = [(cursor.year, cursor.month)]
        else:
            months = rule.get("BYMONTH") or [dtstart.month]
            periods = [(cursor.year, m) for m in sorted(months)]

        for year, month in periods:
            if freq == "MONTHLY" and rule.get("BYMONTH") and month not in rule["BYMONTH"]:
                continue
            if by_year:
                days = _yearly_days(year, rule)
            else:
                days = _monthly_days(year, month, rule, dtstart)
            moments = _apply_times(days, dtstart, rule)
            if setpos:
                moments = _bysetpos(moments, setpos)
            for moment in moments:
                if moment < dtstart:
                    continue
                if until and moment > until:
                    return
                if horizon and moment > horizon:
                    return
                yield moment
                emitted += 1
                if count and emitted >= count:
                    return
                if emitted >= limit:
                    return

        cursor = (
            _add_months(cursor, interval)
            if freq == "MONTHLY"
            else cursor.replace(year=cursor.year + interval)
        )
        # Compare the horizon to where the next period starts, not to the
        # cursor: the cursor keeps DTSTART's day, so for DTSTART on June 15th
        # and BYMONTH=1 it would sit past a horizon in February while that
        # January still had occurrences to give. A YEARLY period may also open
        # in late December, when week 1 straddles the new year.
        if freq == "MONTHLY":
            opening = cursor.replace(day=1)
        else:
            opening = cursor.replace(month=1, day=1) - timedelta(days=7)
        if horizon and opening > horizon:
            return


def _parse_until(raw: str, dtstart: datetime) -> datetime | None:
    from .ics import parse_datetime_value  # import tardif : évite un cycle

    value = parse_datetime_value(raw, tzid=None)
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None and dtstart.tzinfo is not None:
            return value.replace(tzinfo=dtstart.tzinfo)
        if value.tzinfo is not None and dtstart.tzinfo is None:
            return value.replace(tzinfo=None)
        return value
    moment = datetime(value.year, value.month, value.day, 23, 59, 59)
    return moment.replace(tzinfo=dtstart.tzinfo) if dtstart.tzinfo else moment


def last_occurrence(dtstart: datetime, rule: dict) -> datetime | None:
    """Last occurrence of a bounded rule, or None when it is infinite.

    "Bounded" is judged on what the expansion will honour: an UNTIL that does
    not parse bounds nothing, and a series still running when the guard rail
    stops it is treated as endless. Returning that cut-off as its end would
    hide every later occurrence from time-range queries.
    """
    until = rule.get("UNTIL")
    if until and _parse_until(str(until), dtstart) is None:
        until = None
    if not rule.get("COUNT") and not until:
        return None
    last = None
    produced = 0
    try:
        for moment in iter_occurrences(dtstart, rule, limit=MAX_OCCURRENCES):
            last = moment
            produced += 1
    except UnsupportedRule:
        return None
    if produced >= MAX_OCCURRENCES:
        return None
    return last
