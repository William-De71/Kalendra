"""Tests for collection export and vCard import."""

from __future__ import annotations

import unittest

from helpers import ServerTestCase, make_card, make_event
from kalendra.importvcf import importer_cartes, split_vcards
from kalendra.security import csrf_token
from test_feed import RICHE, defauts_de_conformite

PERSO = "/calendars/will/perso/"
CARNET = "/addressbooks/will/amis/"

VCF = (
    "BEGIN:VCARD\r\nVERSION:3.0\r\nUID:c-1\r\nFN:Jean Dupont\r\nEMAIL:jean@example.org\r\n"
    "NOTE:Une note assez longue pour être pliée sur plusieurs lignes physiques dans\r\n"
    "  le fichier exporté\r\nX-ABLABEL:garde-moi\r\nEND:VCARD\r\n"
    "\r\n"
    "begin:vcard\nversion:3.0\nfn:Marie Curie\nemail:marie@example.org\nend:vcard\n"
)


def multipart(champs: dict[str, str], fichier: str) -> tuple[bytes, dict[str, str]]:
    limite = "----KalendraTest"
    morceaux = [
        f'--{limite}\r\nContent-Disposition: form-data; name="{nom}"\r\n\r\n{valeur}\r\n'
        for nom, valeur in champs.items()
    ]
    morceaux.append(
        f'--{limite}\r\nContent-Disposition: form-data; name="fichier"; filename="x.vcf"\r\n'
        f"Content-Type: text/vcard\r\n\r\n{fichier}\r\n--{limite}--\r\n"
    )
    return "".join(morceaux).encode(), {
        "Content-Type": f"multipart/form-data; boundary={limite}"
    }


class ExportTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client.put(f"{PERSO}a.ics", make_event(uid="a"))
        self.client.put(f"{PERSO}r.ics", RICHE.format(uid="r").encode("utf-8"))
        self.carnet_id = self.db.create_addressbook(self.will_id, "amis")
        self.client.request(
            "PUT", f"{CARNET}c.vcf", make_card(uid="c"), {"Content-Type": "text/vcard"}
        )

    def test_export_dun_agenda(self):
        result = self.client.request("GET", "/view/will/perso/?export=ics")
        self.assertStatus(result, 200)
        self.assertTrue(result.header("content-type").startswith("text/calendar"))
        self.assertIn('attachment; filename="perso.ics"', result.header("content-disposition"))
        self.assertIn("UID:a", result.text)
        self.assertIn("UID:r", result.text)
        # A file, not a subscription: no refresh hint.
        self.assertNotIn("REFRESH-INTERVAL", result.text)
        self.assertEqual(defauts_de_conformite(result.body), [])

    def test_export_reimportable(self):
        export = self.client.request("GET", "/view/will/perso/?export=ics").text
        from kalendra.importics import importer

        cible = self.db.get_calendar(self.alice_id, "boulot")
        rapport = importer(self.db, cible, export, max_taille=10**6)
        self.assertEqual(rapport.crees, 2)
        self.assertEqual(rapport.erreurs, [])

    def test_export_dun_carnet(self):
        result = self.client.request("GET", "/view/contacts/will/amis/?export=vcf")
        self.assertStatus(result, 200)
        self.assertTrue(result.header("content-type").startswith("text/vcard"))
        self.assertEqual(result.text.count("BEGIN:VCARD"), 1)
        self.assertTrue(result.text.endswith("END:VCARD\r\n"))

    def test_export_cloisonne(self):
        self.assertStatus(self.alice.request("GET", "/view/will/perso/?export=ics"), 403)
        self.assertStatus(self.alice.request("GET", "/view/contacts/will/amis/?export=vcf"), 403)

    def test_liens_dexport_affiches(self):
        self.assertIn("?export=ics", self.client.request("GET", "/view/will/perso/").text)
        self.assertIn("?export=vcf", self.client.request("GET", "/view/contacts/will/amis/").text)


class VcfSplitTests(unittest.TestCase):
    def test_decoupe_et_conserve_le_pliage(self):
        cartes = split_vcards(VCF)
        self.assertEqual(len(cartes), 2)
        self.assertIn("\r\n  le fichier exporté\r\n", cartes[0])
        self.assertIn("X-ABLABEL:garde-moi", cartes[0])
        self.assertTrue(all(c.endswith("\r\n") for c in cartes))

    def test_rien_a_decouper(self):
        self.assertEqual(split_vcards("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"), [])


class VcfImportTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.carnet_id = self.db.create_addressbook(self.alice_id, "perso")
        self.csrf = csrf_token(self.db.secret_key(), "alice")

    def televerser(self, client, chemin: str, contenu: str, csrf: str | None = None):
        corps, entetes = multipart({"csrf": self.csrf if csrf is None else csrf}, contenu)
        return client.request("POST", chemin, corps, entetes)

    def test_import_depuis_la_vue(self):
        result = self.televerser(self.alice, "/view/contacts/alice/perso/import", VCF)
        self.assertStatus(result, 303)
        self.assertIn("2%20contact", result.header("location"))
        cartes = self.db.list_objects(self.carnet_id)
        self.assertEqual(len(cartes), 2)
        self.assertEqual(sorted(c["summary"] for c in cartes), ["Jean Dupont", "Marie Curie"])
        # Served back by CardDAV exactly as uploaded.
        jean = self.db.get_object_by_uid(self.carnet_id, "c-1")
        get = self.alice.request("GET", f"/addressbooks/alice/perso/{jean['href']}")
        self.assertEqual(get.text, split_vcards(VCF)[0])

    def test_reimport_sans_doublon(self):
        carnet = self.db.get_calendar_by_id(self.carnet_id)
        importer_cartes(self.db, carnet, VCF, max_taille=10**6)
        second = importer_cartes(self.db, carnet, VCF, max_taille=10**6)
        self.assertEqual((second.crees, second.remplaces), (0, 2))
        self.assertEqual(len(self.db.list_objects(self.carnet_id)), 2)

    def test_carte_illisible_signalee_sans_bloquer(self):
        carnet = self.db.get_calendar_by_id(self.carnet_id)
        casse = VCF + "BEGIN:VCARD\r\nFN:Sans fin\r\n"
        rapport = importer_cartes(self.db, carnet, casse, max_taille=10**6)
        self.assertEqual(rapport.crees, 2)

    def test_jeton_csrf_obligatoire(self):
        result = self.televerser(self.alice, "/view/contacts/alice/perso/import", VCF, "faux")
        self.assertStatus(result, 403)
        self.assertEqual(self.db.list_objects(self.carnet_id), [])

    def test_carnet_dun_autre_compte(self):
        self.db.create_addressbook(self.will_id, "amis")
        result = self.televerser(self.alice, "/view/contacts/will/amis/import", VCF)
        self.assertStatus(result, 403)

    def test_formulaire_affiche(self):
        page = self.alice.request("GET", "/view/contacts/alice/perso/").text
        self.assertIn("/view/contacts/alice/perso/import", page)


class CliExportTests(ServerTestCase):
    def run_cli(self, *args: str) -> str:
        import io
        from contextlib import redirect_stdout

        from kalendra.cli import main

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--db", self.config.db_path, *args])
        self.assertEqual(code, 0)
        return buffer.getvalue()

    def test_aller_retour_agenda(self):
        import os

        self.client.put(f"{PERSO}a.ics", make_event(uid="a"))
        fichier = os.path.join(self.tmp, "perso.ics")
        self.run_cli("calendar", "export", "will", "perso", "-o", fichier)
        sortie = self.run_cli("calendar", "import", "alice", "boulot", fichier)
        self.assertIn("1 événement(s) ajouté(s)", sortie)

    def test_aller_retour_carnet(self):
        import os

        self.db.create_addressbook(self.will_id, "amis")
        self.db.create_addressbook(self.alice_id, "amis")
        fichier = os.path.join(self.tmp, "amis.vcf")
        with open(fichier, "w", encoding="utf-8", newline="") as handle:
            handle.write(VCF)
        self.assertIn("2 contact(s)", self.run_cli("addressbook", "import", "will", "amis", fichier))
        export = self.run_cli("addressbook", "export", "will", "amis")
        # Stored as uploaded, lower-case header included.
        self.assertEqual(export.upper().count("BEGIN:VCARD"), 2)


if __name__ == "__main__":
    unittest.main()
