"""Prometheus metrics, in the text exposition format, written by hand.

`prometheus_client` would be a runtime dependency for a few dozen lines of
text; the format is simple and stable enough to produce directly.

Labels are kept to bounded sets on purpose: a method label taken verbatim
from requests would let any client create series at will (an unknown verb per
request), and no label ever carries an account name or a path.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from bisect import bisect_left

from . import __version__

#: Methods counted under their own name; anything else becomes "OTHER".
METHODES = frozenset(
    {
        "GET",
        "HEAD",
        "POST",
        "PUT",
        "DELETE",
        "OPTIONS",
        "PROPFIND",
        "PROPPATCH",
        "REPORT",
        "MKCALENDAR",
        "MKCOL",
    }
)

#: Histogram buckets (seconds): from a cached PROPFIND to a large REPORT.
SEUILS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)


class Metriques:
    """Counters updated by every request; thread-safe, in memory only.

    They start from zero at each restart, which is what Prometheus expects of
    counters: `rate()` and `increase()` handle resets.
    """

    def __init__(self) -> None:
        self._verrou = threading.Lock()
        self._requetes: dict[tuple[str, int], int] = {}
        self._seaux = [0] * (len(SEUILS) + 1)
        self._somme = 0.0
        self._compte = 0
        self.echecs_auth = 0
        self.blocages = 0
        self.debut = time.time()

    def requete(self, methode: str, statut: int, secondes: float) -> None:
        methode = methode.upper() if methode.upper() in METHODES else "OTHER"
        with self._verrou:
            cle = (methode, statut)
            self._requetes[cle] = self._requetes.get(cle, 0) + 1
            self._seaux[bisect_left(SEUILS, secondes)] += 1
            self._somme += secondes
            self._compte += 1

    def echec_auth(self) -> None:
        with self._verrou:
            self.echecs_auth += 1

    def blocage(self) -> None:
        with self._verrou:
            self.blocages += 1

    def exposer(self, db, config, entretien) -> str:
        lignes: list[str] = []

        def metrique(nom: str, genre: str, aide: str, valeurs) -> None:
            lignes.append(f"# HELP {nom} {aide}")
            lignes.append(f"# TYPE {nom} {genre}")
            for etiquettes, valeur in valeurs:
                lignes.append(f"{nom}{etiquettes} {_nombre(valeur)}")

        with self._verrou:
            requetes = sorted(self._requetes.items())
            seaux = list(self._seaux)
            somme, compte = self._somme, self._compte
            echecs, blocages = self.echecs_auth, self.blocages

        metrique(
            "kalendra_info", "gauge", "Version running.", [(f'{{version="{__version__}"}}', 1)]
        )
        metrique(
            "kalendra_start_time_seconds", "gauge", "Process start, Unix time.", [("", self.debut)]
        )
        metrique(
            "kalendra_requests_total",
            "counter",
            "HTTP requests by method and status.",
            [(f'{{method="{m}",status="{s}"}}', n) for (m, s), n in requetes],
        )
        cumul = 0
        histogramme = []
        for seuil, nombre in zip(SEUILS, seaux, strict=False):
            cumul += nombre
            histogramme.append((f'_bucket{{le="{_nombre(seuil)}"}}', cumul))
        histogramme.append(('_bucket{le="+Inf"}', compte))
        histogramme.append(("_sum", somme))
        histogramme.append(("_count", compte))
        lignes.append("# HELP kalendra_request_duration_seconds Request handling time.")
        lignes.append("# TYPE kalendra_request_duration_seconds histogram")
        for suffixe, valeur in histogramme:
            lignes.append(f"kalendra_request_duration_seconds{suffixe} {_nombre(valeur)}")
        metrique(
            "kalendra_auth_failures_total",
            "counter",
            "Rejected credentials (wrong password or unknown account).",
            [("", echecs)],
        )
        metrique(
            "kalendra_auth_throttled_total",
            "counter",
            "Logins refused with 429 by the per-address throttle.",
            [("", blocages)],
        )

        stats = db.stats()
        metrique("kalendra_users", "gauge", "Accounts.", [("", stats["users"])])
        metrique(
            "kalendra_collections",
            "gauge",
            "Collections by kind.",
            [
                ('{kind="calendar"}', stats["calendars"]),
                ('{kind="addressbook"}', stats["addressbooks"]),
            ],
        )
        metrique(
            "kalendra_objects",
            "gauge",
            "Stored objects by component.",
            [
                ('{component="VEVENT"}', stats["events"]),
                ('{component="VTODO"}', stats["todos"]),
                ('{component="VCARD"}', stats["contacts"]),
            ],
        )
        taille = 0
        for suffixe in ("", "-wal"):
            # No -wal file between checkpoints, or an in-memory database.
            with contextlib.suppress(OSError):
                taille += os.path.getsize(config.db_path + suffixe)
        metrique(
            "kalendra_database_size_bytes",
            "gauge",
            "SQLite database size, write-ahead log included.",
            [("", taille)],
        )

        metrique(
            "kalendra_backup_failures_total",
            "counter",
            "Automatic backups that failed since start.",
            [("", entretien.echecs_sauvegarde)],
        )
        if entretien.derniere_sauvegarde is not None:
            # Absent rather than 0 until a backup ran: an alert on
            # "time() - last > 2 days" must not fire at every restart.
            metrique(
                "kalendra_backup_last_success_timestamp_seconds",
                "gauge",
                "Unix time of the latest successful automatic backup.",
                [("", entretien.derniere_sauvegarde)],
            )
            metrique(
                "kalendra_backup_last_size_bytes",
                "gauge",
                "Size of the latest automatic backup.",
                [("", entretien.derniere_sauvegarde_taille)],
            )
        metrique(
            "kalendra_sync_tombstones_purged_total",
            "counter",
            "Sync tombstones purged by the daily maintenance.",
            [("", entretien.tombes_purgees)],
        )
        return "\n".join(lignes) + "\n"


def _nombre(valeur) -> str:
    if isinstance(valeur, float):
        return repr(valeur) if valeur != int(valeur) or abs(valeur) >= 1e15 else f"{valeur:.1f}"
    return str(valeur)
