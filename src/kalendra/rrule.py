"""Expansion des règles de récurrence RFC 5545 (sous-ensemble utile).

Cette implémentation ne sert qu'au filtrage temporel côté serveur
(`<C:time-range>`) et au calcul des bornes indexées. Elle couvre
FREQ=MINUTELY|HOURLY|DAILY|WEEKLY|MONTHLY|YEARLY avec INTERVAL, COUNT,
UNTIL, BYMONTH, BYMONTHDAY, BYDAY (avec position ordinale), BYHOUR,
BYMINUTE, BYSETPOS et WKST.

En cas de règle non gérée, on préfère renvoyer une expansion « ouverte »
(récurrence considérée comme infinie) plutôt que de masquer des événements :
un client CalDAV filtre de toute façon localement, alors qu'un événement
absent de la réponse est un événement perdu pour l'utilisateur.
"""

from __future__ import annotations

import calendar as _calendar
from collections.abc import Iterator
from datetime import date, datetime, timedelta

WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

SUPPORTED_FREQ = {"MINUTELY", "HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY"}

#: Nombre maximal d'occurrences produites par appel (garde-fou anti-boucle).
MAX_OCCURRENCES = 10_000


class UnsupportedRule(ValueError):
    """La règle sort du sous-ensemble géré ; l'appelant doit rester permissif."""


def parse_rrule(value: str) -> dict[str, object]:
    """Décode la valeur d'une propriété RRULE en dictionnaire normalisé."""
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
        elif key in {"BYMONTH", "BYMONTHDAY", "BYHOUR", "BYMINUTE", "BYSETPOS", "BYYEARDAY", "BYWEEKNO"}:
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
    return True


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
    if not monthdays and not bydays:
        days.add(min(base.day, last))

    if monthdays and bydays:
        # Intersection : BYDAY restreint les jours du mois retenus.
        weekdays = {_parse_byday(token)[1] for token in bydays}
        days = {d for d in days if date(year, month, d).weekday() in weekdays}

    return [date(year, month, d) for d in sorted(days)]


def iter_occurrences(
    dtstart: datetime, rule: dict, *, limit: int = MAX_OCCURRENCES, horizon: datetime | None = None
) -> Iterator[datetime]:
    """Produit les occurrences d'une RRULE à partir de `dtstart` (incluse).

    S'arrête à `limit` occurrences, à `UNTIL`/`COUNT`, ou à `horizon`.
    """
    freq = str(rule.get("FREQ", "")).upper()
    if freq not in SUPPORTED_FREQ:
        raise UnsupportedRule(f"FREQ non gérée : {freq or '(absente)'}")
    if rule.get("BYYEARDAY") or rule.get("BYWEEKNO"):
        raise UnsupportedRule("BYYEARDAY / BYWEEKNO non gérés")

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
        return

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
        return

    # MONTHLY et YEARLY
    cursor = dtstart.replace(hour=0, minute=0, second=0, microsecond=0)
    while True:
        guard += 1
        if guard > limit + 2000:
            return
        if freq == "MONTHLY":
            periods = [(cursor.year, cursor.month)]
        else:
            months = rule.get("BYMONTH") or [dtstart.month]
            periods = [(cursor.year, m) for m in sorted(months)]

        for year, month in periods:
            if freq == "MONTHLY" and rule.get("BYMONTH") and month not in rule["BYMONTH"]:
                continue
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
        if horizon and cursor > horizon:
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
    """Dernière occurrence d'une règle bornée, ou None si elle est infinie."""
    if not rule.get("COUNT") and not rule.get("UNTIL"):
        return None
    last = None
    try:
        for moment in iter_occurrences(dtstart, rule, limit=MAX_OCCURRENCES):
            last = moment
    except UnsupportedRule:
        return None
    return last
