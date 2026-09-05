"""Analyseur et générateur iCalendar (RFC 5545), sans dépendance externe.

On n'extrait que ce dont le serveur a besoin : UID, type de composant, bornes
temporelles en secondes UTC et récurrence. Le contenu déposé par le client est
conservé octet pour octet et restitué tel quel — le serveur ne réécrit jamais
un événement, ce qui évite toute perte d'information à la synchronisation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .rrule import UnsupportedRule, iter_occurrences, last_occurrence, parse_rrule

#: Composants stockables comme ressource CalDAV.
OBJECT_COMPONENTS = ("VEVENT", "VTODO", "VJOURNAL", "VFREEBUSY")

#: Garde-fou d'expansion lors d'un filtre temporel.
MAX_EXPANSION = 2000

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
DATETIME_MIN = datetime(1, 1, 2, tzinfo=UTC)
DATETIME_MAX = datetime(9999, 12, 30, tzinfo=UTC)

#: Quelques identifiants de fuseau Microsoft rencontrés chez Outlook/Exchange.
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
    """Le corps fourni n'est pas une ressource CalDAV exploitable."""


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
        """Valeur TEXT déséchappée (RFC 5545 §3.3.11)."""
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
    """Déplie les lignes selon la RFC 5545 §3.1 (continuation par espace/tab)."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for raw in normalized.split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return [line for line in lines if line.strip()]


def parse_content_line(line: str) -> Property | None:
    """Découpe `NOM;PARAM=valeur:contenu` en tenant compte des guillemets."""
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
    """Construit l'arbre de composants d'un objet iCalendar."""
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
    """Résout un TZID en fuseau utilisable, avec repli sur les noms Windows."""
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
    """Décode une valeur DATE ou DATE-TIME (RFC 5545 §3.3.4 et §3.3.5)."""
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
    """Décode l'attribut `start`/`end` d'un `<C:time-range>`."""
    parsed = parse_datetime_value((raw or "").strip())
    return to_unix(parsed)


# ------------------------------------------------------- fenêtres temporelles


def _prop_datetime(component: Component, name: str) -> datetime | date | None:
    prop = component.get(name)
    if prop is None:
        return None
    return parse_datetime_value(prop.value, prop.param("TZID"))


def component_window(component: Component) -> tuple[datetime | None, datetime | None, bool]:
    """Fenêtre [début, fin) d'une occurrence unique. Le booléen indique « journée entière »."""
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
    """Valide une ressource CalDAV et en extrait les métadonnées indexables."""
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
            final = last_occurrence(start, rule)
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
    """Teste précisément la présence d'une occurrence dans [start, end)."""
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
            for moment in iter_occurrences(first, rule, limit=MAX_EXPANSION, horizon=high):
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
    """Implémente `<C:text-match>` (comparaison insensible à la casse)."""
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
    """Une instance concrète d'un objet calendrier, bornée en secondes UTC."""

    start: int
    end: int
    all_day: bool
    summary: str
    uid: str
    href: str = ""


def expand_occurrences(
    data: str,
    start: int | None,
    end: int | None,
    *,
    href: str = "",
    limit: int = 400,
) -> list[Occurrence]:
    """Développe un objet en occurrences chevauchant [start, end).

    Contrairement à `overlaps_range`, qui répond par oui ou non le plus vite
    possible pour filtrer une réponse CalDAV, cette fonction produit les
    instances elles-mêmes : c'est ce qu'il faut pour dessiner un calendrier.

    Les composants portant un `RECURRENCE-ID` remplacent l'occurrence de la
    série à cette date, comme le prévoit la RFC 5545 §3.8.4.4.
    """
    try:
        calendar = parse_calendar(data)
    except InvalidCalendarData:
        return []

    low = from_unix(start) if start is not None else DATETIME_MIN
    high = from_unix(end) if end is not None else DATETIME_MAX

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

    results: list[Occurrence] = []

    def emit(component: Component, begin: datetime, duration: timedelta) -> None:
        finish = begin + duration
        if begin >= high or finish <= low:
            return
        summary_prop = component.get("SUMMARY")
        results.append(
            Occurrence(
                start=to_unix(begin),
                end=to_unix(finish),
                all_day=component_window(component)[2],
                summary=summary_prop.text if summary_prop else "(sans titre)",
                uid=component.value("UID"),
                href=href,
            )
        )

    for component in masters:
        first, last, _ = component_window(component)
        if first is None:
            continue
        duration = (last - first) if last else timedelta()
        excluded = _exception_dates(component)

        for moment in _extra_dates(component):
            if moment not in excluded:
                emit(overrides.get(moment, component), moment, duration)

        rrule_prop = component.get("RRULE")
        if rrule_prop is None:
            if first not in excluded:
                emit(overrides.get(first, component), first, duration)
            continue

        rule = parse_rrule(rrule_prop.value)
        try:
            produced = 0
            for moment in iter_occurrences(first, rule, limit=limit, horizon=high):
                if moment >= high:
                    break
                produced += 1
                if produced > limit:
                    break
                if moment in excluded:
                    continue
                emit(overrides.get(moment, component), moment, duration)
        except UnsupportedRule:
            # Règle non gérée : on montre au moins la première occurrence.
            emit(component, first, duration)

    # Les remplacements peuvent avoir été déplacés hors de la série d'origine.
    for moment, component in overrides.items():
        if any(o.start == to_unix(moment) for o in results):
            continue
        begin, finish, _ = component_window(component)
        if begin is not None:
            emit(component, begin, (finish - begin) if finish else timedelta())

    results.sort(key=lambda o: (o.start, o.summary))
    return results


# --------------------------------------------------------------- génération


def fold(line: str) -> str:
    """Repliage à 75 octets, sans couper un caractère UTF-8 (RFC 5545 §3.1)."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    chunks: list[bytes] = []
    while len(raw) > 75:
        cut = 75
        while cut > 0 and (raw[cut] & 0xC0) == 0x80:
            cut -= 1
        chunks.append(raw[:cut])
        raw = raw[cut:]
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
    """Renvoie les sous-composants de premier niveau, lignes brutes incluses."""
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
            if depth == 2 and current is not None:
                current.append(line)
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
    """Agrège des ressources CalDAV en un VCALENDAR unique publiable.

    Les composants sont recopiés ligne à ligne depuis la ressource d'origine :
    aucune propriété n'est perdue et les VTIMEZONE sont dédupliqués par TZID.
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

# ------------------------------------------------- découpage pour l'import


def split_calendar(texte: str) -> tuple[list[str], list[str]]:
    """Sépare le préambule (entête + VTIMEZONE) des blocs VEVENT/VTODO.

    On travaille sur les lignes dépliées puis on les replie telles quelles :
    reconstruire les objets depuis l'arbre analysé les réécrirait, donc
    perdrait les propriétés que l'analyseur ignore.
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
