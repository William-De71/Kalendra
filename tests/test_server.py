"""Tests against the real HTTP server: proves WebDAV verbs cross the socket."""

from __future__ import annotations

import base64
import http.client
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import make_event
from kalendra import security
from kalendra.app import Kalendra
from kalendra.config import Config
from kalendra.db import Database
from kalendra.server import serve_in_thread

security.PBKDF2_ITERATIONS = 1_000

PROPFIND = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<D:propfind xmlns:D="DAV:"><D:prop><D:current-user-principal/></D:prop></D:propfind>'
)


class LiveServerTests(unittest.TestCase):
    """A real server is started: transport is tested, not just routing."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="kalendra-live-")
        config = Config()
        config.db_path = str(Path(cls.tmp) / "live.db")
        config.bootstrap_admin = ""
        config.bootstrap_password = ""
        db = Database(config.db_path)
        db.setup()
        cls.app = Kalendra(config, db)
        user_id = db.create_user("will", "s3cret", is_admin=True)
        db.create_calendar(user_id, "perso", display_name="Personnel")
        cls.token = db.get_calendar(user_id, "perso")["feed_token"]
        cls.httpd, cls.thread, cls.port = serve_in_thread(cls.app, "127.0.0.1", 0)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def call(self, method: str, path: str, body: bytes = b"", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        merged = {
            "Authorization": "Basic " + base64.b64encode(b"will:s3cret").decode(),
            "Content-Length": str(len(body)),
        }
        merged.update(headers or {})
        try:
            conn.request(method, path, body=body, headers=merged)
            response = conn.getresponse()
            payload = response.read()
            return response.status, payload, dict(response.getheaders())
        finally:
            conn.close()

    def test_options(self):
        status, _, headers = self.call("OPTIONS", "/")
        self.assertEqual(status, 204)
        self.assertIn("calendar-access", headers.get("DAV", ""))

    def test_propfind_passe_le_transport(self):
        status, body, _ = self.call(
            "PROPFIND", "/", PROPFIND.encode(), {"Depth": "0", "Content-Type": "application/xml"}
        )
        self.assertEqual(status, 207)
        self.assertIn(b"/principals/will/", body)

    def test_cycle_put_get_delete(self):
        path = "/calendars/will/perso/live.ics"
        status, _, headers = self.call(
            "PUT", path, make_event(uid="live"), {"Content-Type": "text/calendar"}
        )
        self.assertEqual(status, 201)
        self.assertTrue(headers.get("ETag"))

        status, body, _ = self.call("GET", path)
        self.assertEqual(status, 200)
        self.assertIn(b"UID:live", body)

        status, _, _ = self.call("DELETE", path)
        self.assertEqual(status, 204)

    def test_mkcalendar_et_report(self):
        status, _, _ = self.call("MKCALENDAR", "/calendars/will/live-cal/", b"")
        self.assertEqual(status, 201)
        report = (
            '<?xml version="1.0"?>'
            '<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
            "<D:prop><D:getetag/></D:prop><C:filter>"
            '<C:comp-filter name="VCALENDAR"><C:comp-filter name="VEVENT"/>'
            "</C:comp-filter></C:filter></C:calendar-query>"
        )
        status, body, _ = self.call(
            "REPORT",
            "/calendars/will/live-cal/",
            report.encode(),
            {"Depth": "1", "Content-Type": "application/xml"},
        )
        self.assertEqual(status, 207)
        self.assertIn(b"multistatus", body)

    def test_flux_ics_sans_authentification(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", f"/feed/{self.token}.ics")
            response = conn.getresponse()
            body = response.read()
        finally:
            conn.close()
        self.assertEqual(response.status, 200)
        self.assertTrue(body.startswith(b"BEGIN:VCALENDAR"))

    def test_401_sans_identifiants(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("PROPFIND", "/", body=PROPFIND, headers={"Depth": "0"})
            response = conn.getresponse()
            response.read()
        finally:
            conn.close()
        self.assertEqual(response.status, 401)
        self.assertIn("Basic", response.getheader("WWW-Authenticate", ""))

    def test_sante(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", "/health")
            response = conn.getresponse()
            body = response.read()
        finally:
            conn.close()
        self.assertEqual(response.status, 200)
        self.assertIn(b'"status": "ok"', body)


if __name__ == "__main__":
    unittest.main()
