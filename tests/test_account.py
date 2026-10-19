"""Tests for the "Mon compte" page: client setup, Apple profile, app passwords."""

from __future__ import annotations

import plistlib
import re
import unittest
from urllib.parse import unquote, urlencode

from helpers import Client, ServerTestCase
from kalendra.security import csrf_token

COMPTE = "/view/compte/"


class AccountPageTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.csrf = csrf_token(self.db.secret_key(), "alice")

    def post(self, champs: dict[str, str], client=None, csrf: str | None = None):
        payload = {"csrf": self.csrf if csrf is None else csrf, **champs}
        return (client or self.alice).request(
            "POST",
            COMPTE,
            urlencode(payload).encode(),
            {"Content-Type": "application/x-www-form-urlencoded"},
        )

    def test_page_avec_les_urls_du_compte(self):
        page = self.alice.request("GET", COMPTE, headers={"Host": "cal.example.org"})
        self.assertStatus(page, 200)
        self.assertIn("http://localhost:5232/calendars/alice/", page.text)
        self.assertIn("http://localhost:5232/addressbooks/alice/", page.text)
        self.assertIn("DAVx⁵", page.text)
        self.assertEqual(page.header("cache-control"), "no-store")

    def test_lien_dans_la_barre_laterale(self):
        page = self.alice.request("GET", "/view/").text
        self.assertIn("/view/compte/", page)

    def test_un_utilisateur_nomme_compte_garde_ses_agendas(self):
        uid = self.db.create_user("compte", "x")
        self.db.create_calendar(uid, "perso")
        client = Client(self.app, "compte", "x")
        self.assertStatus(client.request("GET", "/view/compte/perso/"), 200)

    def test_creation_affiche_le_secret_une_fois(self):
        result = self.post({"action": "creer", "label": "téléphone"})
        self.assertStatus(result, 200)
        secret = re.search(r"<code>([A-Za-z0-9_-]{30,})</code>", result.text).group(1)
        self.assertStatus(Client(self.app, "alice", secret).propfind("/", "", "0"), 207)
        # Listed afterwards, but never shown again.
        page = self.alice.request("GET", COMPTE).text
        self.assertIn("téléphone", page)
        self.assertNotIn(secret, page)

    def test_revocation(self):
        app_id, secret = self.db.create_app_password(self.alice_id, "tablette")
        result = self.post({"action": "revoquer", "id": str(app_id)})
        self.assertStatus(result, 303)
        self.assertEqual(self.db.list_app_passwords(self.alice_id), [])
        self.assertStatus(Client(self.app, "alice", secret).propfind("/", "", "0"), 401)

    def test_on_ne_revoque_pas_ceux_dun_autre(self):
        app_id, _ = self.db.create_app_password(self.will_id, "téléphone")
        self.post({"action": "revoquer", "id": str(app_id)})
        self.assertEqual(len(self.db.list_app_passwords(self.will_id)), 1)

    def test_jeton_csrf_obligatoire(self):
        self.assertStatus(self.post({"action": "creer", "label": "x"}, csrf="faux"), 403)
        self.assertEqual(self.db.list_app_passwords(self.alice_id), [])

    def test_libelle_vide(self):
        result = self.post({"action": "creer", "label": "  "})
        self.assertStatus(result, 303)
        self.assertEqual(self.db.list_app_passwords(self.alice_id), [])

    def test_inaccessible_avec_un_mot_de_passe_dapplication(self):
        _, secret = self.db.create_app_password(self.alice_id, "téléphone")
        self.assertStatus(Client(self.app, "alice", secret).request("GET", COMPTE), 401)

    def test_espace_utilise_et_quota(self):
        self.config.quota_bytes = 10 * 1024 * 1024
        page = self.alice.request("GET", COMPTE).text
        self.assertIn("utilisés sur 10,0 Mo", page)


class PasswordChangeTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.csrf = csrf_token(self.db.secret_key(), "alice")

    def changer(self, ancien: str, nouveau: str, confirmation: str | None = None, csrf=None):
        payload = {
            "csrf": self.csrf if csrf is None else csrf,
            "action": "mot_de_passe",
            "ancien": ancien,
            "nouveau": nouveau,
            "confirmation": nouveau if confirmation is None else confirmation,
        }
        return self.alice.request(
            "POST",
            COMPTE,
            urlencode(payload).encode(),
            {"Content-Type": "application/x-www-form-urlencoded"},
        )

    def test_changement_effectif_tout_de_suite(self):
        # Warm the credential cache first: the change must beat it.
        self.assertStatus(self.alice.propfind("/", "", "0"), 207)
        result = self.changer("hunter2", "nouveau-secret")
        self.assertStatus(result, 200)
        self.assertIn("Mot de passe changé", result.text)
        self.assertStatus(self.alice.propfind("/", "", "0"), 401)
        nouveau = Client(self.app, "alice", "nouveau-secret")
        self.assertStatus(nouveau.propfind("/", "", "0"), 207)

    def test_mot_de_passe_actuel_exige(self):
        result = self.changer("faux", "nouveau-secret")
        self.assertStatus(result, 303)
        self.assertIn("incorrect", unquote(result.header("location")))
        self.assertStatus(self.alice.propfind("/", "", "0"), 207)

    def test_confirmation_differente(self):
        self.assertStatus(self.changer("hunter2", "nouveau-secret", "autre-chose"), 303)
        self.assertStatus(self.alice.propfind("/", "", "0"), 207)

    def test_trop_court(self):
        result = self.changer("hunter2", "court")
        self.assertIn("8 caract", unquote(result.header("location")))
        self.assertStatus(self.alice.propfind("/", "", "0"), 207)

    def test_jeton_csrf_obligatoire(self):
        self.assertStatus(self.changer("hunter2", "nouveau-secret", csrf="faux"), 403)
        self.assertStatus(self.alice.propfind("/", "", "0"), 207)

    def test_les_mots_de_passe_dapplication_survivent(self):
        _, secret = self.db.create_app_password(self.alice_id, "téléphone")
        self.changer("hunter2", "nouveau-secret")
        self.assertStatus(Client(self.app, "alice", secret).propfind("/", "", "0"), 207)

    def test_changement_par_ladministrateur_aussi_immediat(self):
        self.assertStatus(self.alice.propfind("/", "", "0"), 207)
        self.db.set_password(self.alice_id, "par-ladmin")
        self.assertStatus(self.alice.propfind("/", "", "0"), 401)


class AppleProfileTests(ServerTestCase):
    def profil(self, public_url: str = "https://cal.example.org") -> dict:
        self.config.public_url = public_url
        result = self.alice.request("GET", f"{COMPTE}?profil=apple")
        self.assertStatus(result, 200)
        self.assertEqual(result.header("content-type"), "application/x-apple-aspen-config")
        self.assertIn("kalendra-alice.mobileconfig", result.header("content-disposition"))
        return plistlib.loads(result.body)

    def test_deux_comptes_caldav_et_carddav(self):
        profil = self.profil()
        types = [p["PayloadType"] for p in profil["PayloadContent"]]
        self.assertEqual(types, ["com.apple.caldav.account", "com.apple.carddav.account"])
        caldav = profil["PayloadContent"][0]
        self.assertEqual(caldav["CalDAVHostName"], "cal.example.org")
        self.assertEqual(caldav["CalDAVPort"], 443)
        self.assertTrue(caldav["CalDAVUseSSL"])
        self.assertEqual(caldav["CalDAVUsername"], "alice")
        self.assertEqual(caldav["CalDAVPrincipalURL"], "/principals/alice/")

    def test_aucun_mot_de_passe_dans_le_profil(self):
        profil = self.profil()
        for compte in profil["PayloadContent"]:
            self.assertFalse(any("Password" in cle for cle in compte))

    def test_sous_chemin_et_port(self):
        profil = self.profil("http://nas.local:8080/cal")
        carddav = profil["PayloadContent"][1]
        self.assertEqual(carddav["CardDAVPort"], 8080)
        self.assertFalse(carddav["CardDAVUseSSL"])
        self.assertEqual(carddav["CardDAVPrincipalURL"], "/cal/principals/alice/")

    def test_identifiants_stables(self):
        self.assertEqual(self.profil()["PayloadUUID"], self.profil()["PayloadUUID"])


if __name__ == "__main__":
    unittest.main()
