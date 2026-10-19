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


class MaintenanceTests(ServerTestCase):
    def test_prochaine_echeance(self):
        from datetime import datetime

        from kalendra.maintenance import prochaine_echeance

        self.assertEqual(
            prochaine_echeance(datetime(2026, 10, 19, 1, 30), 3), datetime(2026, 10, 19, 3)
        )
        self.assertEqual(
            prochaine_echeance(datetime(2026, 10, 19, 3, 0), 3), datetime(2026, 10, 20, 3)
        )
        self.assertEqual(
            prochaine_echeance(datetime(2026, 12, 31, 23, 0), 3), datetime(2027, 1, 1, 3)
        )

    def test_rotation_ne_touche_que_ses_fichiers(self):
        from datetime import datetime, timedelta

        from kalendra.maintenance import sauvegarder

        dossier = os.path.join(self.tmp, "sauvegardes")
        os.makedirs(dossier)
        with open(os.path.join(dossier, "a-garder.txt"), "w") as handle:
            handle.write("pas une sauvegarde")
        debut = datetime(2026, 10, 1, 3)
        for jour in range(5):
            sauvegarder(self.db, dossier, garder=3, maintenant=debut + timedelta(days=jour))
        fichiers = sorted(os.listdir(dossier))
        self.assertEqual(
            fichiers,
            [
                "a-garder.txt",
                "kalendra-20261003-030000.db",
                "kalendra-20261004-030000.db",
                "kalendra-20261005-030000.db",
            ],
        )

    def test_entretien_complet(self):
        from kalendra.maintenance import entretien

        self.config.backup_dir = os.path.join(self.tmp, "auto")
        self.config.sync_retention_days = 90
        self.client.put(f"{PERSO}a.ics", make_event(uid="a"))
        self.client.request("DELETE", f"{PERSO}a.ics")
        self.db.conn.execute("UPDATE changes SET changed_at = datetime('now', '-200 days')")
        etat = entretien(self.db, self.config)
        self.assertEqual(etat.erreurs, [])
        self.assertEqual(etat.tombes_purgees, 1)
        self.assertIsNotNone(etat.derniere_sauvegarde)
        self.assertEqual(len(os.listdir(self.config.backup_dir)), 1)

    def test_un_echec_est_consigne_sans_lever(self):
        from kalendra.maintenance import entretien

        # A file where the directory should be: the backup cannot be written.
        bloque = os.path.join(self.tmp, "fichier")
        with open(bloque, "w") as handle:
            handle.write("x")
        self.config.backup_dir = bloque
        with self.assertLogs("kalendra.maintenance", "ERROR"):
            etat = entretien(self.db, self.config)
        self.assertEqual(etat.echecs_sauvegarde, 1)
        self.assertIsNone(etat.derniere_sauvegarde)
        self.assertTrue(etat.erreurs)

    def test_cli(self):
        dossier = os.path.join(self.tmp, "cli")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--db", self.config.db_path, "maintenance", "--backup-dir", dossier])
        self.assertEqual(code, 0)
        self.assertIn("Sauvegarde : faite", buffer.getvalue())
        self.assertEqual(len(os.listdir(dossier)), 1)

    def test_le_fil_ne_demarre_quune_fois(self):
        self.config.maintenance_hour = 3
        self.app.start_maintenance()
        premier = self.app._planificateur
        self.app.start_maintenance()
        try:
            self.assertIs(self.app._planificateur, premier)
            self.assertTrue(premier.is_alive())
            self.assertTrue(premier.daemon)
        finally:
            premier.arreter()
            premier.join(timeout=5)
        self.assertFalse(premier.is_alive())


class MetricsTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config.metrics = True

    def scrape(self, headers=None) -> str:
        result = self.anon.request("GET", "/metrics", headers=headers)
        self.assertStatus(result, 200)
        self.assertTrue(result.header("content-type").startswith("text/plain; version=0.0.4"))
        return result.text

    def valeur(self, texte: str, ligne: str) -> float:
        for brute in texte.splitlines():
            if brute.startswith(ligne + " "):
                return float(brute.rsplit(" ", 1)[1])
        raise AssertionError(f"{ligne} absent")

    def test_desactive_par_defaut(self):
        self.config.metrics = False
        self.assertStatus(self.anon.request("GET", "/metrics"), 404)

    def test_requetes_et_echecs_comptes(self):
        self.client.propfind(PERSO, "", depth="0")
        self.client.propfind(PERSO, "", depth="0")
        Client(self.app, "will", "faux").request("PROPFIND", PERSO)
        texte = self.scrape()
        self.assertEqual(
            self.valeur(texte, 'kalendra_requests_total{method="PROPFIND",status="207"}'), 2
        )
        self.assertEqual(self.valeur(texte, "kalendra_auth_failures_total"), 1)
        self.assertEqual(self.valeur(texte, 'kalendra_collections{kind="calendar"}'), 2)
        self.assertGreater(self.valeur(texte, "kalendra_database_size_bytes"), 0)
        self.assertIn('kalendra_request_duration_seconds_bucket{le="+Inf"}', texte)

    def test_methodes_inconnues_regroupees(self):
        # One series per invented verb would let any client grow the series
        # count without bound.
        for verbe in ("FOO", "BAR", "BAZ"):
            self.client.request(verbe, "/")
        texte = self.scrape()
        self.assertNotIn('method="FOO"', texte)
        self.assertIn('method="OTHER"', texte)

    def test_aucun_nom_de_compte_ni_chemin(self):
        self.client.propfind(PERSO, "", depth="0")
        texte = self.scrape()
        self.assertNotIn("will", texte)
        self.assertNotIn("/calendars", texte)

    def test_jeton(self):
        self.config.metrics_token = "s3cr3t-scrape"
        refus = self.anon.request("GET", "/metrics")
        self.assertStatus(refus, 401)
        self.assertIn("Bearer", refus.header("www-authenticate"))
        self.assertStatus(
            self.anon.request("GET", "/metrics", headers={"Authorization": "Bearer faux"}), 401
        )
        self.scrape({"Authorization": "Bearer s3cr3t-scrape"})

    def test_sauvegarde_exposee_apres_la_premiere(self):
        self.assertNotIn("kalendra_backup_last_success_timestamp_seconds", self.scrape())
        from kalendra.maintenance import entretien

        self.config.backup_dir = os.path.join(self.tmp, "auto")
        entretien(self.db, self.config, self.app.maintenance)
        self.assertGreater(
            self.valeur(self.scrape(), "kalendra_backup_last_success_timestamp_seconds"), 0
        )


class ThrottleTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app.throttle.max_failures = 3
        self.app.throttle.window = 300
        self.app.auth_cache.clear()

    def call(self, user: str, password: str, ip: str = "203.0.113.7", headers=None):
        client = Client(self.app, user, password)
        merged = {"authorization": client.auth, **(headers or {})}
        return self.app.dispatch(Request("PROPFIND", PERSO, headers=merged, remote_addr=ip))

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

    def test_pas_dinjection_de_ligne(self):
        # A client controls the path, the username and X-Forwarded-For: none
        # of them may start a new log line.
        self.config.trust_proxy = True
        requete = Request(
            "GET",
            "/x\r\nip=1.2.3.4 method=GET path=/admin status=200",
            headers={"x-forwarded-for": "6.6.6.6\n"},
        )
        with self.assertLogs("kalendra.access", "INFO") as journal:
            self.app.dispatch(requete)
        self.assertEqual(len(journal.output), 1)
        self.assertNotIn("\n", journal.output[0])
        self.assertNotIn("\r", journal.output[0])
        self.assertIn("\\r\\nip=1.2.3.4", journal.output[0])

    def test_for_log(self):
        from kalendra.http import for_log

        self.assertEqual(for_log("a\r\nb\x00c\x1bd"), "a\\r\\nb\\x00c\\x1bd")
        self.assertEqual(for_log("élève /ok"), "élève /ok")

    def test_desactivable(self):
        self.config.access_log = False
        with self.assertNoLogs("kalendra.access", "INFO"):
            self.client.request("OPTIONS", "/")


class AppPasswordTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app_id, self.secret = self.db.create_app_password(self.will_id, "téléphone")
        self.phone = Client(self.app, "will", self.secret)

    def test_ouvre_caldav(self):
        self.assertStatus(self.phone.propfind(PERSO, "", depth="0"), 207)
        self.assertIsNotNone(self.db.get_app_password(self.app_id)["last_used_at"])

    def test_seule_lempreinte_est_stockee(self):
        rows = self.db.conn.execute("SELECT token_hash FROM app_passwords").fetchall()
        self.assertNotIn(self.secret, [r[0] for r in rows])

    def test_ne_donne_pas_acces_a_linterface_web(self):
        self.assertStatus(self.phone.request("GET", "/view/"), 401)
        self.assertStatus(self.phone.request("GET", "/admin"), 401)
        # The account password still does.
        self.assertStatus(self.client.request("GET", "/view/"), 200)

    def test_revocation_immediate_malgre_le_cache(self):
        self.assertStatus(self.phone.propfind(PERSO, "", depth="0"), 207)
        self.assertTrue(self.db.delete_app_password(self.will_id, self.app_id))
        self.assertStatus(self.phone.propfind(PERSO, "", depth="0"), 401)

    def test_lie_a_son_compte(self):
        self.assertStatus(Client(self.app, "alice", self.secret).propfind("/", "", "0"), 401)
        # Revoking through another account's id does nothing.
        self.assertFalse(self.db.delete_app_password(self.alice_id, self.app_id))

    def test_le_mot_de_passe_du_compte_reste_valable(self):
        self.assertStatus(self.client.propfind(PERSO, "", depth="0"), 207)

    def test_libelle_obligatoire(self):
        with self.assertRaises(ValueError):
            self.db.create_app_password(self.will_id, "  ")

    def test_migration_dune_base_v3(self):
        chemin = os.path.join(self.tmp, "ancienne.db")
        conn = sqlite3.connect(chemin)
        conn.executescript(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,"
            " password_hash TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '',"
            " email TEXT NOT NULL DEFAULT '', is_admin INTEGER NOT NULL DEFAULT 0,"
            " created_at TEXT NOT NULL); PRAGMA user_version = 3;"
        )
        conn.close()
        from kalendra.db import SCHEMA_VERSION, connect, init_db

        conn = connect(chemin)
        try:
            init_db(conn)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            conn.execute("SELECT * FROM app_passwords").fetchall()
        finally:
            conn.close()

    def test_migration_dune_base_v4(self):
        # The changes and calendars tables as v4 created them: no changed_at,
        # no sync_floor. A tombstone in them must survive the migration dated.
        chemin = os.path.join(self.tmp, "v4.db")
        conn = sqlite3.connect(chemin)
        conn.executescript(
            "CREATE TABLE changes (id INTEGER PRIMARY KEY, calendar_id INTEGER NOT NULL,"
            " href TEXT NOT NULL, sync_rev INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,"
            " UNIQUE (calendar_id, href));"
            "CREATE TABLE calendars (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
            " kind TEXT NOT NULL DEFAULT 'calendar', name TEXT NOT NULL,"
            " display_name TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',"
            " color TEXT NOT NULL DEFAULT '#3584e4', sort_order INTEGER NOT NULL DEFAULT 0,"
            " timezone TEXT NOT NULL DEFAULT '', components TEXT NOT NULL DEFAULT 'VEVENT,VTODO',"
            " sync_rev INTEGER NOT NULL DEFAULT 1, feed_token TEXT UNIQUE,"
            " feed_enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);"
            "INSERT INTO changes (calendar_id, href, sync_rev, deleted) VALUES (1, 'x.ics', 3, 1);"
            "PRAGMA user_version = 4;"
        )
        conn.close()
        from kalendra.db import connect, init_db

        conn = connect(chemin)
        try:
            init_db(conn)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 5)
            date = conn.execute("SELECT changed_at FROM changes").fetchone()[0]
            self.assertIsNotNone(date)
            self.assertEqual(conn.execute("SELECT sync_floor FROM calendars").fetchall(), [])
        finally:
            conn.close()

    def test_cli(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            main(["--db", self.config.db_path, "app-password", "add", "alice", "tablette"])
        secret = buffer.getvalue().splitlines()[1]
        self.assertStatus(Client(self.app, "alice", secret).propfind("/", "", "0"), 207)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            main(["--db", self.config.db_path, "app-password", "list", "alice"])
        self.assertIn("tablette", buffer.getvalue())
        app_id = buffer.getvalue().split()[0]
        with redirect_stdout(io.StringIO()):
            main(["--db", self.config.db_path, "app-password", "rm", "alice", app_id])
        self.assertEqual(self.db.list_app_passwords(self.alice_id), [])


class QuotaTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.taille = len(make_event(uid="q0"))
        # Room for two events, not three.
        self.config.quota_bytes = self.taille * 2 + self.taille // 2

    def test_refus_au_dela_du_quota(self):
        self.assertStatus(self.client.put(f"{PERSO}1.ics", make_event(uid="q1")), 201)
        self.assertStatus(self.client.put(f"{PERSO}2.ics", make_event(uid="q2")), 201)
        refus = self.client.put(f"{PERSO}3.ics", make_event(uid="q3"))
        self.assertStatus(refus, 507)
        self.assertIn("quota-not-exceeded", refus.text)

    def test_remplacer_un_objet_ne_compte_que_la_difference(self):
        self.client.put(f"{PERSO}1.ics", make_event(uid="q1"))
        self.client.put(f"{PERSO}2.ics", make_event(uid="q2"))
        self.assertStatus(self.client.put(f"{PERSO}2.ics", make_event(uid="q2")), 204)

    def test_le_quota_est_celui_du_compte_proprietaire(self):
        self.client.put(f"{PERSO}1.ics", make_event(uid="q1"))
        self.client.put(f"{PERSO}2.ics", make_event(uid="q2"))
        # Alice's account is empty: her calendar still has room.
        self.assertStatus(
            self.alice.put("/calendars/alice/boulot/1.ics", make_event(uid="a1")), 201
        )

    def test_quota_disponible_annonce(self):
        self.client.put(f"{PERSO}1.ics", make_event(uid="q1"))
        reponse = self.client.propfind(
            PERSO,
            "<?xml version='1.0'?><d:propfind xmlns:d='DAV:'><d:prop>"
            "<d:quota-available-bytes/><d:quota-used-bytes/></d:prop></d:propfind>",
        )
        restant = self.config.quota_bytes - self.taille
        self.assertIn(f"quota-available-bytes>{restant}<", reponse.text)

    def test_sans_quota_rien_nest_annonce(self):
        self.config.quota_bytes = 0
        reponse = self.client.propfind(
            PERSO,
            "<?xml version='1.0'?><d:propfind xmlns:d='DAV:'><d:prop>"
            "<d:quota-available-bytes/></d:prop></d:propfind>",
        )
        self.assertIn("404 Not Found", reponse.text)

    def test_limport_sarrete_au_quota(self):
        from kalendra.importics import importer

        blocs = "".join(
            f"BEGIN:VEVENT\r\nUID:i{n}\r\nDTSTAMP:20260101T000000Z\r\n"
            f"DTSTART:20260310T090000Z\r\nSUMMARY:x\r\nEND:VEVENT\r\n"
            for n in range(10)
        )
        contenu = f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\n{blocs}END:VCALENDAR\r\n"
        calendrier = self.db.get_calendar_by_id(self.perso_id)
        rapport = importer(self.db, calendrier, contenu, max_taille=10**6, quota=500)
        self.assertGreater(rapport.crees, 0)
        self.assertGreater(rapport.ignores, 0)
        self.assertLessEqual(self.db.account_usage(self.will_id), 500)
        self.assertIn("quota", rapport.erreurs[0])


if __name__ == "__main__":
    unittest.main()
