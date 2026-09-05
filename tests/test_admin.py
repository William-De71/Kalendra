"""Tests de l'interface d'administration et du CLI."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from urllib.parse import urlencode

from helpers import ServerTestCase
from kalendra.cli import main
from kalendra.security import csrf_token


class AdminUITests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.csrf = csrf_token(self.db.secret_key(), "will")

    def post(self, path: str, fields: dict[str, str], csrf: str | None = None):
        payload = dict(fields)
        payload["csrf"] = self.csrf if csrf is None else csrf
        return self.client.request(
            "POST",
            path,
            urlencode(payload).encode("utf-8"),
            {"Content-Type": "application/x-www-form-urlencoded"},
        )

    def test_tableau_de_bord_liste_utilisateurs_et_flux(self):
        result = self.client.request("GET", "/admin", headers={"Accept": "text/html"})
        self.assertStatus(result, 200)
        self.assertIn("will", result.text)
        self.assertIn("alice", result.text)
        self.assertIn("/calendars/will/perso/", result.text)
        self.assertIn("/feed/", result.text)

    def test_racine_redirige_vers_ladmin_dans_un_navigateur(self):
        result = self.client.request("GET", "/", headers={"Accept": "text/html"})
        self.assertStatus(result, 302)
        self.assertEqual(result.header("location"), "/admin")

    def test_un_compte_non_admin_est_refuse(self):
        self.assertStatus(self.alice.request("GET", "/admin"), 403)

    def test_creation_dutilisateur(self):
        result = self.post("/admin/users/create", {"username": "bob", "password": "motdepasse"})
        self.assertStatus(result, 303)
        self.assertIsNotNone(self.db.get_user("bob"))

    def test_creation_dagenda(self):
        result = self.post(
            "/admin/calendars/create",
            {"user_id": str(self.will_id), "name": "astreinte", "display_name": "Astreinte"},
        )
        self.assertStatus(result, 303)
        calendar = self.db.get_calendar(self.will_id, "astreinte")
        self.assertEqual(calendar["display_name"], "Astreinte")

    def test_rotation_du_jeton_de_flux(self):
        avant = self.db.get_calendar_by_id(self.perso_id)["feed_token"]
        self.post("/admin/calendars/token", {"calendar_id": str(self.perso_id)})
        self.assertNotEqual(avant, self.db.get_calendar_by_id(self.perso_id)["feed_token"])

    def test_bascule_du_flux(self):
        self.post("/admin/calendars/feed", {"calendar_id": str(self.perso_id)})
        self.assertEqual(self.db.get_calendar_by_id(self.perso_id)["feed_enabled"], 0)

    def test_jeton_csrf_obligatoire(self):
        result = self.post(
            "/admin/users/create", {"username": "mallory", "password": "x"}, csrf="faux"
        )
        self.assertStatus(result, 403)
        self.assertIsNone(self.db.get_user("mallory"))

    def test_suppression_du_dernier_compte_refusee(self):
        self.db.delete_user(self.alice_id)
        result = self.post("/admin/users/delete", {"user_id": str(self.will_id)})
        self.assertStatus(result, 303)  # redirection avec message d'erreur
        self.assertIsNotNone(self.db.get_user("will"))

    def test_ui_desactivable(self):
        self.config.admin_ui = False
        self.assertStatus(self.client.request("GET", "/admin"), 404)


class CliTests(ServerTestCase):
    def run_cli(self, *args: str) -> str:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--db", self.config.db_path, *args])
        self.assertEqual(code, 0)
        return buffer.getvalue()

    def test_creation_dun_compte_et_dun_agenda(self):
        self.run_cli("user", "add", "bob", "--password", "motdepasse", "--with-calendar", "perso")
        user = self.db.get_user("bob")
        self.assertIsNotNone(user)
        self.assertEqual([c["name"] for c in self.db.list_calendars(user["id"])], ["perso"])

    def test_listing_des_agendas(self):
        output = self.run_cli("calendar", "list")
        self.assertIn("/calendars/will/perso/", output)

    def test_rotation_du_jeton_en_cli(self):
        avant = self.db.get_calendar_by_id(self.perso_id)["feed_token"]
        output = self.run_cli("calendar", "token", "will", "perso")
        self.assertTrue(output.startswith("/feed/"))
        self.assertNotIn(avant, output)

    def test_changement_de_mot_de_passe(self):
        self.run_cli("user", "passwd", "will", "--password", "nouveau-mdp")
        from helpers import Client

        self.app.auth_cache.clear()
        self.assertStatus(Client(self.app, "will", "nouveau-mdp").request("OPTIONS", "/"), 204)


if __name__ == "__main__":
    unittest.main()
