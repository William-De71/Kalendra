"""Tests for operating features: backup, login throttling, access log, app
passwords and quotas."""

from __future__ import annotations

import io
import os
import sqlite3
import unittest
from contextlib import redirect_stdout

from helpers import Client, ServerTestCase, make_event
from kalendra.app import client_ip
from kalendra.cli import main
from kalendra.http import Request

PERSO = "/calendars/will/perso/"


def _compter(chemin: str, table: str) -> int:
    # Plain sqlite3 rather than Database: the latter keeps one connection per
    # thread, and pointing it at the copy would leak the suite's own.
    conn = sqlite3.connect(chemin)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


class BackupTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.assertStatus(self.client.put(f"{PERSO}a.ics", make_event(uid="a")), 201)

    def test_la_copie_contient_les_donnees_et_se_suffit(self):
        cible = os.path.join(self.tmp, "copie.db")
        taille = self.db.backup(cible)
        self.assertEqual(taille, os.path.getsize(cible))
        # No -wal sidecar must be needed to read the copy.
        self.assertFalse(os.path.exists(cible + "-wal"))
        self.assertEqual(_compter(cible, "users"), 2)
        self.assertEqual(_compter(cible, "objects"), 1)

    def test_la_copie_nest_pas_en_mode_wal(self):
        cible = os.path.join(self.tmp, "copie.db")
        self.db.backup(cible)
        conn = sqlite3.connect(cible)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        finally:
            conn.close()

    def test_version_compactee(self):
        cible = os.path.join(self.tmp, "compacte.db")
        self.db.backup(cible, compact=True)
        self.assertEqual(_compter(cible, "objects"), 1)

    def test_refuse_decraser_un_fichier(self):
        cible = os.path.join(self.tmp, "copie.db")
        self.db.backup(cible)
        with self.assertRaises(FileExistsError):
            self.db.backup(cible)

    def test_cli_dans_un_dossier(self):
        dossier = os.path.join(self.tmp, "sauvegardes")
        os.makedirs(dossier)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--db", self.config.db_path, "backup", dossier])
        self.assertEqual(code, 0)
        fichiers = os.listdir(dossier)
        self.assertEqual(len(fichiers), 1)
        self.assertTrue(fichiers[0].startswith("kalendra-") and fichiers[0].endswith(".db"))
        self.assertIn("Sauvegarde écrite", buffer.getvalue())


class ThrottleTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app.throttle.max_failures = 3
        self.app.throttle.window = 300
        self.app.auth_cache.clear()

    def call(self, user: str, password: str, ip: str = "203.0.113.7", headers=None):
        client = Client(self.app, user, password)
        merged = {"authorization": client.auth, **(headers or {})}
        return self.app.dispatch(
            Request("PROPFIND", PERSO, headers=merged, remote_addr=ip)
        )

    def test_blocage_apres_trop_dechecs(self):
        for _ in range(3):
            self.assertEqual(self.call("will", "faux").status, 401)
        bloque = self.call("will", "s3cret")
        self.assertEqual(bloque.status, 429)
        retry = dict(bloque.headers)["Retry-After"]
        self.assertGreater(int(retry), 0)

    def test_les_comptes_inexistants_comptent_aussi(self):
        for _ in range(3):
            self.call("personne", "x")
        self.assertEqual(self.call("will", "s3cret").status, 429)

    def test_une_autre_adresse_nest_pas_touchee(self):
        for _ in range(3):
            self.call("will", "faux")
        self.assertEqual(self.call("will", "s3cret", ip="198.51.100.1").status, 207)

    def test_un_succes_remet_le_compteur_a_zero(self):
        for _ in range(2):
            self.call("will", "faux")
        self.assertEqual(self.call("will", "s3cret").status, 207)
        self.app.auth_cache.clear()
        for _ in range(2):
            self.call("will", "faux")
        self.assertEqual(self.call("will", "s3cret").status, 207)

    def test_une_requete_sans_identifiants_nest_pas_un_echec(self):
        for _ in range(10):
            self.app.dispatch(Request("PROPFIND", PERSO, remote_addr="203.0.113.7"))
        self.assertEqual(self.call("will", "s3cret").status, 207)

    def test_identifiants_en_cache_passent_malgre_le_blocage(self):
        self.assertEqual(self.call("will", "s3cret").status, 207)
        for _ in range(3):
            self.call("will", "faux")
        self.assertEqual(self.call("will", "s3cret").status, 207)

    def test_desactivable(self):
        self.app.throttle.max_failures = 0
        for _ in range(10):
            self.call("will", "faux")
        self.assertEqual(self.call("will", "s3cret").status, 207)

    def test_adresse_derriere_un_proxy(self):
        requete = Request(
            "GET", "/", headers={"x-forwarded-for": "1.2.3.4, 10.0.0.9"}, remote_addr="10.0.0.1"
        )
        self.assertEqual(client_ip(requete, trust_proxy=False), "10.0.0.1")
        # The rightmost entry is the one our proxy appended; the client chose
        # the others.
        self.assertEqual(client_ip(requete, trust_proxy=True), "10.0.0.9")


class AccessLogTests(ServerTestCase):
    def test_une_ligne_par_requete_avec_le_compte(self):
        with self.assertLogs("kalendra.access", "INFO") as journal:
            self.client.propfind(PERSO, "", depth="0")
        self.assertEqual(len(journal.output), 1)
        ligne = journal.output[0]
        for attendu in ("method=PROPFIND", f"path={PERSO}", "status=207", "user=will", "ms="):
            self.assertIn(attendu, ligne)

    def test_le_jeton_de_flux_nest_jamais_journalise(self):
        token = self.db.get_calendar_by_id(self.perso_id)["feed_token"]
        with self.assertLogs("kalendra.access", "INFO") as journal:
            self.anon.request("GET", f"/feed/{token}.ics")
        self.assertNotIn(token, "".join(journal.output))
        self.assertIn("path=/feed/…", journal.output[0])

    def test_echec_dauthentification_journalise_sans_compte(self):
        with self.assertLogs("kalendra.access", "INFO") as journal:
            Client(self.app, "will", "faux").request("PROPFIND", PERSO)
        self.assertIn("status=401", journal.output[0])
        self.assertIn("user=-", journal.output[0])

    def test_desactivable(self):
        self.config.access_log = False
        with self.assertNoLogs("kalendra.access", "INFO"):
            self.client.request("OPTIONS", "/")


if __name__ == "__main__":
    unittest.main()
