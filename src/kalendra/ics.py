"""iCalendar parser and generator (RFC 5545), with no external dependency.

Only what the server needs is extracted: UID, component type, time bounds in
UTC seconds and recurrence. What the client uploads is stored byte for byte and
served back unchanged — the server never rewrites an event, which rules out any
loss of information on sync.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .rrule import UnsupportedRule, iter_occurrences, last_occurrence, parse_rrule

#: Components storable as a CalDAV resource.
OBJECT_COMPONENTS = ("VEVENT", "VTODO", "VJOURNAL", "VFREEBUSY")

#: Expansion guard rail during a time-range filter.
MAX_EXPANSION = 2000

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
DATETIME_MIN = datetime(1, 1, 2, tzinfo=UTC)
DATETIME_MAX = datetime(9999, 12, 30, tzinfo=UTC)

#: A few Microsoft time-zone identifiers seen from Outlook/Exchange.
WINDOWS_TZ = {
    "ROMANCE STANDARD TIME": "Europe/Paris",
    "W. EUROPE STANDARD TIME": "Europe/Berlin",
    "CENTRAL EUROPE STANDARD TIME": "Europe/Budapest",
    "CENTRAL EUROPEAN STANDARD TIME": "Europe/Warsaw",
    "GMT STANDARD TIME": "Europe/London",
    "GREENWICH STANDARD TIME": "Atlantic/Reykjavik",
    "UTC": "UTC",
    "EASTERN STANDARD TIME": "America/New_York",
    "CENTRAL STANDARD TIME": "America/Chicago",
    "MOUNTAIN STANDARD TIME": "America/Denver",
    "PACIFIC STANDARD TIME": "America/Los_Angeles",
}

_DURATION_RE = re.compile(
    r"^(?P<sign>[+-])?P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


class InvalidCalendarData(ValueError):
    """The supplied body is not a usable CalDAV resource."""


# ------------------------------------------------------------------- modèle


@dataclass(slots=True)
class Property:
    name: str
    params: dict[str, str]
    value: str

    def param(self, key: str, default: str = "") -> str:
        return self.params.get(key.upper(), default)

    @property
    def text(self) -> str:
        """Unescaped TEXT value (RFC 5545 §3.3.11)."""
        out: list[str] = []
        escaped = False
        for char in self.value:
            if escaped:
                out.append({"n": "\n", "N": "\n"}.get(char, char))
                escaped = False
            elif char == "\\":
                escaped = True
            else:
                out.append(char)
        return "".join(out)


@dataclass(slots=True)
class Component:
    name: str
    props: dict[str, list[Property]] = field(default_factory=dict)
    children: list[Component] = field(default_factory=list)

    def get(self, name: str) -> Property | None:
        values = self.props.get(name.upper())
        return values[0] if values else None

    def all(self, name: str) -> list[Property]:
        return self.props.get(name.upper(), [])

    def value(self, name: str, default: str = "") -> str:
        prop = self.get(name)
        return prop.value if prop is not None else default

    def walk(self, name: str | None = None):
        if name is None or self.name == name.upper():
            yield self
        for child in self.children:
            yield from child.walk(name)


# ------------------------------------------------------------------ analyse


def unfold(text: str) -> list[str]:
    """Unfold lines per RFC 5545 §3.1 (continuation by space/tab)."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for raw in normalized.split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return [line for line in lines if line.strip()]


def parse_content_line(line: str) -> Property | None:
    """Split `NAME;PARAM=value:content`, honouring quoted strings."""
    in_quotes = False
    separator = -1
    for index, char in enumerate(line):
        if char == '"':
            in_quotes = not in_quotes
        elif char == ":" and not in_quotes:
            separator = index
            break
    if separator < 0:
        return None

    head, value = line[:separator], line[separator + 1 :]
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    for char in head:
        if char == '"':
            in_quotes = not in_quotes
            current.append(char)
        elif char == ";" and not in_quotes:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))

    name = parts[0].strip().upper()
    params: dict[str, str] = {}
    for chunk in parts[1:]:
        key, _, raw = chunk.partition("=")
        params[key.strip().upper()] = raw.strip().strip('"')
    return Property(name=name, params=params, value=value)


def parse_calendar(text: str) -> Component:
    """Build the component tree of an iCalendar object."""
    root: Component | None = None
    stack: list[Component] = []

    for line in unfold(text):
        prop = parse_content_line(line)
        if prop is None:
            continue
        if prop.name == "BEGIN":
            component = Component(name=prop.value.strip().upper())
            if stack:
                stack[-1].children.append(component)
            elif root is None:
                root = component
            else:
                raise InvalidCalendarData("plusieurs composants racine")
            stack.append(component)
        elif prop.name == "END":
            if not stack or stack[-1].name != prop.value.strip().upper():
                raise InvalidCalendarData(f"END:{prop.value} inattendu")
            stack.pop()
        else:
            if not stack:
                continue
            stack[-1].props.setdefault(prop.name, []).append(prop)

    if root is None:
        raise InvalidCalendarData("aucun composant trouvé")
    if stack:
        raise InvalidCalendarData(f"composant {stack[-1].name} non fermé")
    return root


# ------------------------------------------------------------- valeurs typées


def resolve_timezone(tzid: str) -> timezone | ZoneInfo | None:
    """Resolve a TZID to a usable zone, falling back to Windows names."""
    if not tzid:
        return None
    candidate = tzid.strip().strip('"')
    if candidate.startswith("/"):
        # Forme « /freeassociation.sourceforge.net/Europe/Paris »
        parts = [p for p in candidate.split("/") if p]
        candidate = "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        pass
    mapped = WINDOWS_TZ.get(candidate.upper())
    if mapped:
        try:
            return ZoneInfo(mapped)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            return None
    return None


def parse_datetime_value(raw: str, tzid: str | None = None) -> datetime | date | None:
    """Decode a DATE or DATE-TIME value (RFC 5545 §3.3.4 and §3.3.5)."""
    if not raw:
        return None
    value = raw.strip()
    if value.endswith("Z"):
        try:
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError:
            return None
    if "T" in value:
        try:
            moment = datetime.strptime(value, "%Y%m%dT%H%M%S")
        except ValueError:
            return None
        tz = resolve_timezone(tzid or "")
        return moment.replace(tzinfo=tz) if tz is not None else moment
    try:
        parsed = datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return None
    return parsed.date()


def parse_duration(raw: str) -> timedelta | None:
    match = _DURATION_RE.match((raw or "").strip().upper())
    if not match:
        return None
    groups = {k: int(v) for k, v in match.groupdict().items() if v and k != "sign"}
    delta = timedelta(
        weeks=groups.get("weeks", 0),
        days=groups.get("days", 0),
        hours=groups.get("hours", 0),
        minutes=groups.get("minutes", 0),
        seconds=groups.get("seconds", 0),
    )
    return -delta if match.group("sign") == "-" else delta


def to_utc(value: datetime | date | None) -> datetime | None:
    """Normalise DATE / DATE-TIME (y compris flottant) en datetime UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return None


def to_unix(value: datetime | date | None) -> int | None:
    moment = to_utc(value)
    return None if moment is None else int((moment - EPOCH).total_seconds())


def from_unix(value: int) -> datetime:
    return EPOCH + timedelta(seconds=int(value))


def ical_utc(value: int | datetime) -> str:
    moment = from_unix(value) if isinstance(value, int) else to_utc(value)
    return moment.strftime("%Y%m%dT%H%M%SZ")


def parse_range_value(raw: str | None) -> int | None:
    """Decode the `start`/`end` attribute of a `<C:time-range>`."""
    parsed = parse_datetime_value((raw or "").strip())
    return to_unix(parsed)


# ------------------------------------------------------- fenêtres temporelles


def _prop_datetime(component: Component, name: str) -> datetime | date | None:
    prop = component.get(name)
    if prop is None:
        return None
    return parse_datetime_value(prop.value, prop.param("TZID"))


def component_window(component: Component) -> tuple[datetime | None, datetime | None, bool]:
    """Window [start, end) of a single occurrence. The flag marks all-day."""
    start_prop = component.get("DTSTART")
    raw_start = _prop_datetime(component, "DTSTART")
    all_day = raw_start is not None and not isinstance(raw_start, datetime)
    if start_prop is not None and start_prop.param("VALUE").upper() == "DATE":
        all_day = True

    start = to_utc(raw_start)
    end = to_utc(_prop_datetime(component, "DTEND"))
    if end is None:
        end = to_utc(_prop_datetime(component, "DUE"))
    if end is None and start is not None:
        duration = parse_duration(component.value("DURATION"))
        if duration is not None:
            end = start + duration
        else:
            end = start + (timedelta(days=1) if all_day else timedelta())
    if start is None and end is not None:
        start = end
    return start, end, all_day


def _exception_dates(component: Component) -> set[datetime]:
    excluded: set[datetime] = set()
    for prop in component.all("EXDATE"):
        for chunk in prop.value.split(","):
            moment = to_utc(parse_datetime_value(chunk, prop.param("TZID")))
            if moment is not None:
                excluded.add(moment)
    return excluded


def _extra_dates(component: Component) -> list[datetime]:
    extra: list[datetime] = []
    for prop in component.all("RDATE"):
        for chunk in prop.value.split(","):
            moment = to_utc(parse_datetime_value(chunk.split("/")[0], prop.param("TZID")))
            if moment is not None:
                extra.append(moment)
    return extra


def local_start(component: Component) -> datetime | None:
    """DTSTART as written — in its own zone — which is where a rule recurs.

    RFC 5545 expands a rule in DTSTART's time zone: "every Monday at 10:00
    Europe/Paris" stays at 10:00 across daylight saving time, so 09:00 UTC in
    winter and 08:00 UTC in summer. Expanding the UTC instant instead would
    shift every summer occurrence by an hour, and EXDATE and RECURRENCE-ID
    would stop matching. A DATE or a floating time stays naive, which `to_utc`
    reads as UTC, like everywhere else.
    """
    raw = _prop_datetime(component, "DTSTART")
    if raw is None:
        return None
    if not isinstance(raw, datetime):
        return datetime(raw.year, raw.month, raw.day)
    return raw


def recurrences(
    component: Component, rule: dict, *, limit: int, horizon: datetime | None = None
):
    """UTC occurrences of a rule, expanded in DTSTART's own zone."""
    start = local_start(component)
    if start is None:
        return
    if horizon is not None and start.tzinfo is None:
        # Naive (floating) times cannot be compared with an aware horizon.
        horizon = horizon.astimezone(UTC).replace(tzinfo=None)
    for moment in iter_occurrences(start, rule, limit=limit, horizon=horizon):
        yield to_utc(moment)


def last_recurrence(component: Component, rule: dict) -> datetime | None:
    start = local_start(component)
    if start is None:
        return None
    final = last_occurrence(start, rule)
    return to_utc(final) if final is not None else None


# -------------------------------------------------------------- métadonnées


@dataclass(slots=True)
class ObjectMeta:
    uid: str
    component: str
    start: int | None
    end: int | None
    recurring: bool
    summary: str


def parse_object(data: str) -> ObjectMeta:
    """Validate a CalDAV resource and extract its indexable metadata."""
    if not data or not data.strip():
        raise InvalidCalendarData("corps vide")

    calendar = parse_calendar(data)
    if calendar.name != "VCALENDAR":
        raise InvalidCalendarData("le composant racine doit être VCALENDAR")

    components = [c for c in calendar.children if c.name in OBJECT_COMPONENTS]
    if not components:
        raise InvalidCalendarData("aucun composant VEVENT/VTODO/VJOURNAL/VFREEBUSY")

    uids = {c.value("UID").strip() for c in components if c.value("UID").strip()}
    if not uids:
        raise InvalidCalendarData("UID manquant")
    if len(uids) > 1:
        raise InvalidCalendarData("une ressource ne peut porter qu'un seul UID")

    kind = components[0].name
    starts: list[datetime] = []
    ends: list[datetime] = []
    unbounded = False
    recurring = False
    summary = ""

    for component in components:
        if not summary:
            summary = component.get("SUMMARY").text if component.get("SUMMARY") else ""
        start, end, _ = component_window(component)
        if start is not None:
            starts.append(start)
        duration = (end - start) if (start and end) else timedelta()

        rrule_prop = component.get("RRULE")
        if rrule_prop is not None and start is not None:
            recurring = True
            rule = parse_rrule(rrule_prop.value)
            final = last_recurrence(component, rule)
            if final is None:
                unbounded = True
            else:
                ends.append(final + duration)
        elif end is not None:
            ends.append(end)

        for extra in _extra_dates(component):
            recurring = True
            starts.append(extra)
            ends.append(extra + duration)

    return ObjectMeta(
        uid=next(iter(uids)),
        component=kind,
        start=to_unix(min(starts)) if starts else None,
        end=None if unbounded or not ends else to_unix(max(ends)),
        recurring=recurring,
        summary=summary[:200],
    )


def overlaps_range(data: str, start: int | None, end: int | None) -> bool:
    """Test precisely whether an occurrence falls within [start, end)."""
    if start is None and end is None:
        return True
    try:
        calendar = parse_calendar(data)
    except InvalidCalendarData:
        return True  # en cas de doute on montre plutôt que de masquer

    low = from_unix(start) if start is not None else DATETIME_MIN
    high = from_unix(end) if end is not None else DATETIME_MAX

    for component in calendar.children:
        if component.name not in OBJECT_COMPONENTS:
            continue
        first, last, _ = component_window(component)
        if first is None:
            continue
        duration = (last - first) if last else timedelta()
        excluded = _exception_dates(component)

        for moment in _extra_dates(component):
            if moment not in excluded and moment < high and moment + duration > low:
                return True

        rrule_prop = component.get("RRULE")
        if rrule_prop is None:
            if first < high and (last or first) > low:
                return True
            continue

        rule = parse_rrule(rrule_prop.value)
        try:
            count = 0
            for moment in recurrences(component, rule, limit=MAX_EXPANSION, horizon=high):
                if moment >= high:
                    break
                count += 1
                if moment in excluded:
                    continue
                if moment + duration > low:
                    return True
                if count >= MAX_EXPANSION:
                    return True
        except UnsupportedRule:
            return True
    return False


def text_matches(data: str, component_name: str, prop: str, needle: str, negate: bool) -> bool:
    """Implement `<C:text-match>` (case-insensitive comparison)."""
    try:
        calendar = parse_calendar(data)
    except InvalidCalendarData:
        return False
    found = False
    for component in calendar.walk(component_name):
        for candidate in component.all(prop):
            if needle.lower() in candidate.text.lower():
                found = True
                break
        if found:
            break
    return (not found) if negate else found


# ------------------------------------------------------------- occurrences


@dataclass(slots=True)
class Occurrence:
    """A concrete instance of a calendar object, bounded in UTC seconds."""

    start: int
    end: int
    all_day: bool
    summary: str
    uid: str
    href: str = ""


@dataclass(slots=True)
class Instance:
    """One occurrence, still tied to the component that describes it."""

    component: Component
    start: datetime
    duration: timedelta
    #: Original start in the series, for RECURRENCE-ID; None for a lone event.
    recurrence_id: datetime | None


def _split_overrides(calendar: Component) -> tuple[list[Component], dict[datetime, Component]]:
    masters: list[Component] = []
    overrides: dict[datetime, Component] = {}
    for component in calendar.children:
        if component.name not in OBJECT_COMPONENTS:
            continue
        recurrence_id = component.get("RECURRENCE-ID")
        if recurrence_id is not None:
            moment = to_utc(
                parse_datetime_value(recurrence_id.value, recurrence_id.param("TZID"))
            )
            if moment is not None:
                overrides[moment] = component
                continue
        masters.append(component)
    return masters, overrides


def iter_instances(
    calendar: Component, start: int | None, end: int | None, *, limit: int = 400
) -> list[Instance]:
    """Instances of a parsed object overlapping [start, end), sorted by start.

    Components carrying a `RECURRENCE-ID` override the series occurrence at
    that date, as RFC 5545 §3.8.4.4 prescribes.
    """
    low = from_unix(start) if start is not None else DATETIME_MIN
    high = from_unix(end) if end is not None else DATETIME_MAX
    masters, overrides = _split_overrides(calendar)
    results: list[Instance] = []
    used: set[datetime] = set()

    def emit(
        component: Component, begin: datetime, duration: timedelta, rid, substitute: bool = True
    ) -> None:
        if substitute and rid is not None and rid in overrides:
            used.add(rid)
            component = overrides[rid]
            # The override carries its own time: it may have been moved.
            own_start, own_end, _ = component_window(component)
            if own_start is not None:
                begin = own_start
                duration = (own_end - own_start) if own_end else timedelta()
        # RFC 4791 §9.9: a zero-length instance overlaps when it starts inside.
        finish = begin + duration
        if begin >= high or (finish <= low and not (duration == timedelta() and begin >= low)):
            return
        results.append(Instance(component, begin, duration, rid))

    for component in masters:
        first, last, _ = component_window(component)
        if first is None:
            continue
        duration = (last - first) if last else timedelta()
        excluded = _exception_dates(component)
        rrule_prop = component.get("RRULE")
        recurrent = rrule_prop is not None or bool(component.all("RDATE"))

        for moment in _extra_dates(component):
            if moment not in excluded:
                emit(component, moment, duration, moment)

        if rrule_prop is None:
            if first not in excluded:
                emit(component, first, duration, first if recurrent else None)
            continue

        rule = parse_rrule(rrule_prop.value)
        try:
            produced = 0
            for moment in recurrences(component, rule, limit=limit, horizon=high):
                if moment >= high:
                    break
                produced += 1
                if produced > limit:
                    break
                if moment in excluded:
                    continue
                emit(component, moment, duration, moment)
        except UnsupportedRule:
            # Unsupported rule: show at least the first occurrence.
            emit(component, first, duration, first)

    # Overrides may have been moved outside the original series, or into the
    # range from an occurrence outside it.
    for moment, component in overrides.items():
        if moment in used:
            continue
        begin, finish, _ = component_window(component)
        if begin is not None:
            duration = (finish - begin) if finish else timedelta()
            emit(component, begin, duration, moment, substitute=False)

    results.sort(key=lambda i: (i.start, i.component.value("SUMMARY")))
    return results


def expand_occurrences(
    data: str,
    start: int | None,
    end: int | None,
    *,
    href: str = "",
    limit: int = 400,
) -> list[Occurrence]:
    """Expand an object into occurrences overlapping [start, end).

    Unlike `overlaps_range`, which answers yes or no as fast as it can in order
    to filter a CalDAV response, this function produces the instances
    themselves: that is what drawing a calendar requires.
    """
    try:
        calendar = parse_calendar(data)
    except InvalidCalendarData:
        return []

    results = []
    for instance in iter_instances(calendar, start, end, limit=limit):
        summary_prop = instance.component.get("SUMMARY")
        results.append(
            Occurrence(
                start=to_unix(instance.start),
                end=to_unix(instance.start + instance.duration),
                all_day=component_window(instance.component)[2],
                summary=summary_prop.text if summary_prop else "(sans titre)",
                uid=instance.component.value("UID"),
                href=href,
            )
        )
    results.sort(key=lambda o: (o.start, o.summary))
    return results


# ------------------------------------------------- expansion côté serveur


#: Properties `<C:expand>` recomputes for each instance; every other line of
#: the component is copied as stored.
_EXPANDED = {"DTSTART", "DTEND", "DUE", "RRULE", "RDATE", "EXDATE", "EXRULE", "RECURRENCE-ID"}


def _split_raw(text: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """VCALENDAR header lines and raw top-level blocks, in document order."""
    header: list[str] = []
    depth = 0
    for line in unfold(text):
        upper = line.upper()
        if upper.startswith("BEGIN:"):
            depth += 1
        elif upper.startswith("END:"):
            depth -= 1
        elif depth == 1:
            header.append(line)
    return header, iter_top_level_blocks(text)


def _stamp(moment: datetime, all_day: bool, name: str) -> str:
    if all_day:
        return f"{name};VALUE=DATE:{moment:%Y%m%d}"
    return f"{name}:{ical_utc(moment)}"


def _instance_lines(raw: list[str], instance: Instance, all_day: bool) -> list[str]:
    own = {
        line.split(":", 1)[0].split(";", 1)[0].upper()
        for line in raw[1:-1]
    }
    lines = [raw[0], _stamp(instance.start, all_day, "DTSTART")]
    finish = instance.start + instance.duration
    if "DTEND" in own:
        lines.append(_stamp(finish, all_day, "DTEND"))
    elif "DUE" in own:
        lines.append(_stamp(finish, all_day, "DUE"))
    if instance.recurrence_id is not None:
        lines.append(_stamp(instance.recurrence_id, all_day, "RECURRENCE-ID"))
    depth = 0
    for line in raw[1:-1]:
        upper = line.upper()
        if upper.startswith("BEGIN:"):
            depth += 1
        elif upper.startswith("END:"):
            depth -= 1
        elif depth == 0 and line.split(":", 1)[0].split(";", 1)[0].upper() in _EXPANDED:
            continue
        lines.append(line)
    lines.append(raw[-1])
    return lines


def expand_calendar(data: str, start: int | None, end: int | None) -> str:
    """Answer `<C:expand>` (RFC 4791 §9.6.5): one component per instance.

    The stored object is not touched: this is a view computed for one
    response. Each instance keeps every line of its component except the
    recurrence properties, replaced by its own DTSTART/DTEND in UTC and a
    RECURRENCE-ID. VTIMEZONE blocks are dropped, the RFC requiring UTC.
    When anything is in doubt, the object goes out as stored.
    """
    try:
        calendar = parse_calendar(data)
        header, blocks = _split_raw(data)
    except InvalidCalendarData:
        return data
    if len(blocks) != len(calendar.children):
        return data
    raw_of = {id(c): lines for c, (_, lines) in zip(calendar.children, blocks, strict=True)}

    body: list[str] = []
    for component in calendar.children:
        if component.name in OBJECT_COMPONENTS and component_window(component)[0] is None:
            body.extend(raw_of[id(component)])  # nothing to expand: as stored
    for instance in iter_instances(calendar, start, end, limit=MAX_EXPANSION):
        all_day = component_window(instance.component)[2]
        body.extend(_instance_lines(raw_of[id(instance.component)], instance, all_day))
    lines = ["BEGIN:VCALENDAR", *header, *body, "END:VCALENDAR"]
    return "\r\n".join(fold(line) for line in lines) + "\r\n"


def limit_recurrence_set(data: str, start: int | None, end: int | None) -> str:
    """Answer `<C:limit-recurrence-set>` (RFC 4791 §9.6.6).

    The master component stays whole; overridden instances that neither start
    nor originally started within the range are left out.
    """
    try:
        calendar = parse_calendar(data)
        _, blocks = _split_raw(data)
    except InvalidCalendarData:
        return data
    if len(blocks) != len(calendar.children):
        return data
    low = from_unix(start) if start is not None else DATETIME_MIN
    high = from_unix(end) if end is not None else DATETIME_MAX

    dropped: set[int] = set()
    for index, component in enumerate(calendar.children):
        rid = component.get("RECURRENCE-ID")
        if component.name not in OBJECT_COMPONENTS or rid is None:
            continue
        original = to_utc(parse_datetime_value(rid.value, rid.param("TZID")))
        begin, finish, _ = component_window(component)
        inside = original is not None and low <= original < high
        if begin is not None and begin < high and (finish or begin) >= low:
            inside = True
        if not inside:
            dropped.add(index)
    if not dropped:
        return data

    # Rebuilt from raw lines minus the dropped blocks, everything else in order.
    kept = [lines for i, (_, lines) in enumerate(blocks) if i not in dropped]
    header, _ = _split_raw(data)
    lines = ["BEGIN:VCALENDAR", *header, *(line for block in kept for line in block)]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"


# --------------------------------------------------------------- génération


def fold(line: str) -> str:
    """Fold at 75 bytes without splitting a UTF-8 character (RFC 5545 §3.1)."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    chunks: list[bytes] = []
    # The leading space of a continuation line counts towards its 75 octets:
    # 74 bytes of content after the first line, or strict consumers balk.
    limit = 75
    while len(raw) > limit:
        cut = limit
        while cut > 0 and (raw[cut] & 0xC0) == 0x80:
            cut -= 1
        chunks.append(raw[:cut])
        raw = raw[cut:]
        limit = 74
    chunks.append(raw)
    return "\r\n ".join(chunk.decode("utf-8") for chunk in chunks)


def escape_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace(",", r"\,")
        .replace("\n", r"\n")
    )


def iter_top_level_blocks(text: str) -> list[tuple[str, list[str]]]:
    """Return top-level sub-components, raw lines included."""
    blocks: list[tuple[str, list[str]]] = []
    depth = 0
    current: list[str] | None = None
    name = ""
    for line in unfold(text):
        upper = line.upper()
        if upper.startswith("BEGIN:"):
            depth += 1
            if depth == 2:
                name = line.split(":", 1)[1].strip().upper()
                current = [line]
                continue
        elif upper.startswith("END:"):
            if current is not None:
                # Nested ends too (END:VALARM): dropping them would publish a
                # BEGIN:VALARM never closed, and the whole feed rejected.
                current.append(line)
                if depth == 2:
                    blocks.append((name, current))
                    current = None
            depth -= 1
            continue
        if current is not None:
            current.append(line)
    return blocks


def build_feed(
    *,
    name: str,
    description: str,
    color: str,
    objects: list[str],
    refresh_minutes: int,
    prodid: str,
) -> str:
    """Aggregate CalDAV resources into a single publishable VCALENDAR.

    Components are copied line by line from the original resource: no property
    is lost, and VTIMEZONE blocks are de-duplicated by TZID.
    """
    interval = max(5, int(refresh_minutes))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{prodid}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        fold(f"X-WR-CALNAME:{escape_text(name)}"),
        f"REFRESH-INTERVAL;VALUE=DURATION:PT{interval}M",
        f"X-PUBLISHED-TTL:PT{interval}M",
    ]
    if description:
        lines.append(fold(f"X-WR-CALDESC:{escape_text(description)}"))
    if color:
        lines.append(f"X-APPLE-CALENDAR-COLOR:{color}")

    seen_tz: set[str] = set()
    timezones: list[str] = []
    body: list[str] = []

    for raw in objects:
        try:
            blocks = iter_top_level_blocks(raw)
        except Exception:
            continue
        for block_name, block_lines in blocks:
            if block_name == "VTIMEZONE":
                tzid = ""
                for line in block_lines:
                    if line.upper().startswith("TZID:"):
                        tzid = line.split(":", 1)[1].strip()
                        break
                if not tzid or tzid in seen_tz:
                    continue
                seen_tz.add(tzid)
                timezones.extend(fold(line) for line in block_lines)
            elif block_name in OBJECT_COMPONENTS:
                body.extend(fold(line) for line in block_lines)

    return "\r\n".join([*lines, *timezones, *body, "END:VCALENDAR", ""])

# ------------------------------------------------------ splitting for import


def split_calendar(texte: str) -> tuple[list[str], list[str]]:
    """Separate the preamble (header + VTIMEZONE) from VEVENT/VTODO blocks.

    Work happens on unfolded lines that are then rejoined as-is: rebuilding the
    objects from the parsed tree would rewrite them, and so lose the properties
    the parser ignores.
    """
    preambule: list[str] = []
    blocs: list[list[str]] = []
    courant: list[str] | None = None
    dans_timezone = False

    for ligne in unfold(texte):
        nom = ligne.split(":", 1)[0].split(";", 1)[0].upper()
        valeur = ligne.split(":", 1)[1].strip().upper() if ":" in ligne else ""

        if nom == "BEGIN" and valeur == "VTIMEZONE":
            dans_timezone = True
            preambule.append(ligne)
            continue
        if dans_timezone:
            preambule.append(ligne)
            if nom == "END" and valeur == "VTIMEZONE":
                dans_timezone = False
            continue

        if nom == "BEGIN" and valeur in {"VEVENT", "VTODO", "VJOURNAL"}:
            courant = [ligne]
            continue
        if courant is not None:
            courant.append(ligne)
            if nom == "END" and valeur in {"VEVENT", "VTODO", "VJOURNAL"}:
                blocs.append(courant)
                courant = None
            continue

        entete = nom in {"BEGIN", "VERSION", "PRODID", "CALSCALE", "METHOD"} or nom.startswith("X-")
        if entete and not (nom == "BEGIN" and valeur != "VCALENDAR"):
            preambule.append(ligne)

    return preambule, ["\r\n".join(bloc) for bloc in blocs]


def wrap_component(preambule: list[str], bloc: str) -> str:
    lignes = [*preambule, bloc, "END:VCALENDAR"]
    return "\r\n".join(lignes) + "\r\n"
