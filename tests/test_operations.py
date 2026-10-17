"""Tests for operating features: backup, login throttling, access log, app
passwords and quotas."""

from __future__ import annotations

import io
import os
import sqlite3
import unittest
from contextlib import redirect_stdout

from helpers import ServerTestCase, make_event
from kalendra.cli import main
from kalendra.db import Database

PERSO = "/calendars/will/perso/"


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
        copie = Database(cible)
        self.assertEqual(copie.get_user("will")["username"], "will")
        self.assertEqual(len(copie.list_objects(self.perso_id)), 1)

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
        self.assertEqual(len(Database(cible).list_objects(self.perso_id)), 1)

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


if __name__ == "__main__":
    unittest.main()
