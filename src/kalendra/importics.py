"""Import d'un fichier iCalendar dans un agenda existant.

Un `.ics` publié (vacances scolaires, jours fériés, calendrier d'un club)
agrège tous ses événements dans un seul `VCALENDAR`, alors que CalDAV impose
une ressource par événement. On découpe donc le fichier et on dépose un objet
par composant, en recopiant l'entête et les `VTIMEZONE` dans chacun.

Le contenu n'est pas réécrit au-delà de ce découpage : les propriétés que
l'analyseur ignore traversent l'import intactes, comme pour un `PUT` normal.

Cette fonction ne fait aucune requête réseau. Kalendra n'a pas de client HTTP
sortant : le fichier arrive par le formulaire, jamais par une URL que le
serveur irait chercher lui-même.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ics import InvalidCalendarData, parse_object, split_calendar, wrap_component
from .security import etag_for

#: Garde-fou : au-delà, l'import est refusé plutôt que de bloquer un thread.
MAX_COMPOSANTS = 5000


@dataclass(slots=True)
class Rapport:
    """Résultat d'un import, tel qu'affiché à l'utilisateur."""

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
    """Dépose chaque composant de `contenu` dans l'agenda `calendar_row`.

    Le nom de ressource dérive de l'UID : réimporter le même fichier met à jour
    les objets au lieu de les dupliquer. Un composant illisible est signalé et
    ignoré, sans interrompre le reste — mieux vaut importer 67 événements sur
    68 que d'échouer entièrement sur une ligne mal formée.
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

        # Un UID déjà présent sous un autre nom de ressource viendrait d'un
        # import antérieur nommé autrement : on écrase cette ressource-là
        # plutôt que d'en créer une seconde pour le même événement.
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
    """Nom de ressource dérivé de l'UID, filtré sur le jeu sûr de `SAFE_HREF`."""
    base = "".join(c if c.isalnum() or c in "._-" else "-" for c in uid).strip("-")
    if not base:
        base = f"import-{index}"
    return f"{base[:200]}.ics"
