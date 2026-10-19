"""Daily maintenance: automatic backups and purging of old sync tombstones.

A backup left to a host cron job is a backup someone forgets to set up — in
a Docker deployment, the host is often not the person's to configure. So the
server runs it itself, once a day at a fixed local hour, when
`KALENDRA_BACKUP_DIR` names a directory. The same pass purges sync tombstones
older than `KALENDRA_SYNC_RETENTION_DAYS`.

The thread only exists under `kalendra serve` (and the ASGI lifespan): the
test suite and the CLI build applications without one. `kalendra maintenance`
runs the same pass once, by hand or from a cron job after all.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

logger = logging.getLogger("kalendra.maintenance")

#: Only files matching this are ever deleted by the rotation: a backup
#: directory may hold anything else the administrator puts there.
MOTIF_SAUVEGARDE = re.compile(r"^kalendra-\d{8}-\d{6}\.db$")


@dataclass
class EtatEntretien:
    """Outcome of the latest passes, read by the /metrics endpoint."""

    derniere_sauvegarde: float | None = None
    derniere_sauvegarde_taille: int = 0
    echecs_sauvegarde: int = 0
    tombes_purgees: int = 0
    dernier_passage: float | None = None
    erreurs: list[str] = field(default_factory=list)


def nom_sauvegarde(maintenant: datetime) -> str:
    return f"kalendra-{maintenant:%Y%m%d-%H%M%S}.db"


def sauvegarder(db, dossier: str, garder: int, maintenant: datetime | None = None) -> str:
    """Write a backup into `dossier`, then keep only the `garder` most recent.

    The timestamped name sorts chronologically, which is what the rotation
    relies on. `garder` <= 0 keeps every backup.
    """
    maintenant = maintenant or datetime.now()
    os.makedirs(dossier, exist_ok=True)
    chemin = os.path.join(dossier, nom_sauvegarde(maintenant))
    db.backup(chemin)
    if garder > 0:
        sauvegardes = sorted(f for f in os.listdir(dossier) if MOTIF_SAUVEGARDE.match(f))
        for ancienne in sauvegardes[:-garder]:
            os.remove(os.path.join(dossier, ancienne))
            logger.info("ancienne sauvegarde supprimée : %s", ancienne)
    return chemin


def entretien(db, config, etat: EtatEntretien | None = None) -> EtatEntretien:
    """One maintenance pass. Never raises: a failure is logged and recorded.

    Raising here would kill the background thread, and with it every later
    backup — a silent failure mode far worse than one missed night.
    """
    etat = etat if etat is not None else EtatEntretien()
    etat.erreurs = []
    if config.sync_retention_days > 0:
        try:
            purgees = db.purge_changes(config.sync_retention_days)
            etat.tombes_purgees += purgees
            if purgees:
                logger.info("%d suppression(s) anciennes oubliées du journal de synchro", purgees)
        except Exception as exc:  # pragma: no cover - depends on the disk
            logger.exception("purge du journal de synchronisation impossible")
            etat.erreurs.append(f"purge : {exc}")
    if config.backup_dir:
        try:
            chemin = sauvegarder(db, config.backup_dir, config.backup_keep)
            etat.derniere_sauvegarde = time.time()
            etat.derniere_sauvegarde_taille = os.path.getsize(chemin)
            logger.info("sauvegarde écrite : %s", chemin)
        except Exception as exc:
            etat.echecs_sauvegarde += 1
            logger.exception("sauvegarde automatique impossible")
            etat.erreurs.append(f"sauvegarde : {exc}")
    etat.dernier_passage = time.time()
    return etat


def prochaine_echeance(maintenant: datetime, heure: int) -> datetime:
    """Next occurrence of `heure`:00 local time, strictly after `maintenant`."""
    cible = maintenant.replace(hour=heure % 24, minute=0, second=0, microsecond=0)
    if cible <= maintenant:
        cible += timedelta(days=1)
    return cible


class Planificateur(threading.Thread):
    """Daemon thread running `entretien` once a day."""

    def __init__(self, db, config, etat: EtatEntretien) -> None:
        super().__init__(name="kalendra-maintenance", daemon=True)
        self.db = db
        self.config = config
        self.etat = etat
        self._arret = threading.Event()

    def run(self) -> None:
        while not self._arret.is_set():
            echeance = prochaine_echeance(datetime.now(), self.config.maintenance_hour)
            # Waited in short slices rather than all at once: a laptop that
            # sleeps through the night would otherwise wake up hours late.
            while not self._arret.is_set() and datetime.now() < echeance:
                self._arret.wait(min(60.0, (echeance - datetime.now()).total_seconds()))
            if self._arret.is_set():
                return
            entretien(self.db, self.config, self.etat)

    def arreter(self) -> None:
        self._arret.set()
