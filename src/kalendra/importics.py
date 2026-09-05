"""Import of an iCalendar file into an existing calendar.

A published `.ics` (school holidays, public holidays, a club's calendar)
aggregates all its events into a single `VCALENDAR`, whereas CalDAV requires one
resource per event. The file is therefore split and one object stored per
component, copying the header and `VTIMEZONE` blocks into each.

Content is not rewritten beyond that split: properties the parser ignores pass
through the import untouched, exactly as with a normal `PUT`.

This function makes no network request. Kalendra has no outbound HTTP client:
the file arrives through the form, never through a URL the server would fetch
itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ics import InvalidCalendarData, parse_object, split_calendar, wrap_component
from .security import etag_for

#: Guard rail: beyond this, the import is refused rather than tying up a thread.
MAX_COMPOSANTS = 5000


@dataclass(slots=True)
class Rapport:
    """Outcome of an import, as shown to the user."""

    crees: int = 0
    remplaces: int = 0
    ignores: int = 0
    erreurs: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.erreurs is None:
            self.erreurs = []

    @property
    def total(self) -> int:
        return self.crees + self.remplaces

    def resume(self) -> str:
        morceaux = []
        if self.crees:
            morceaux.append(f"{self.crees} événement(s) ajouté(s)")
        if self.remplaces:
            morceaux.append(f"{self.remplaces} mis à jour")
        if self.ignores:
            morceaux.append(f"{self.ignores} ignoré(s)")
        if not morceaux:
            return "Aucun événement importé."
        return ", ".join(morceaux) + "."


def importer(db, calendar_row, contenu: str, *, max_taille: int) -> Rapport:
    """Store every component of `contenu` into the `calendar_row` calendar.

    The resource name derives from the UID: re-importing the same file updates
    objects instead of duplicating them. An unreadable component is reported and
    skipped without stopping the rest — importing 67 events out of 68 beats
    failing entirely over one malformed line.
    """
    rapport = Rapport()

    preambule, blocs = split_calendar(contenu)
    if not blocs:
        rapport.erreurs.append("Aucun événement trouvé dans ce fichier.")
        return rapport
    if len(blocs) > MAX_COMPOSANTS:
        rapport.erreurs.append(
            f"Fichier trop volumineux : {len(blocs)} événements, "
            f"maximum {MAX_COMPOSANTS}."
        )
        return rapport

    composants_admis = {
        c.strip().upper() for c in str(calendar_row["components"]).split(",") if c.strip()
    }

    for index, bloc in enumerate(blocs, start=1):
        objet = wrap_component(preambule, bloc)
        if len(objet.encode("utf-8")) > max_taille:
            rapport.ignores += 1
            rapport.erreurs.append(f"Événement {index} : trop volumineux.")
            continue

        try:
            meta = parse_object(objet)
        except InvalidCalendarData as exc:
            rapport.ignores += 1
            rapport.erreurs.append(f"Événement {index} : {exc}")
            continue

        if composants_admis and meta.component not in composants_admis:
            rapport.ignores += 1
            rapport.erreurs.append(
                f"Événement {index} : {meta.component} refusé par cet agenda."
            )
            continue

        href = _href_pour(meta.uid, index)
        existant = db.get_object(calendar_row["id"], href)

        # A UID already present under another resource name would come from an
        # earlier import named differently: overwrite that resource rather than
        # create a second one for the same event.
        conflit = db.get_object_by_uid(calendar_row["id"], meta.uid)
        if conflit is not None and conflit["href"] != href:
            href = conflit["href"]
            existant = conflit

        db.put_object(
            calendar_row["id"],
            href,
            objet,
            uid=meta.uid,
            component=meta.component,
            dtstart=meta.start,
            dtend=meta.end,
            recurring=meta.recurring,
            summary=meta.summary,
            etag=etag_for(objet),
        )
        if existant is None:
            rapport.crees += 1
        else:
            rapport.remplaces += 1

    return rapport


def _href_pour(uid: str, index: int) -> str:
    """Resource name derived from the UID, filtered to the safe `SAFE_HREF` set."""
    base = "".join(c if c.isalnum() or c in "._-" else "-" for c in uid).strip("-")
    if not base:
        base = f"import-{index}"
    return f"{base[:200]}.ics"
