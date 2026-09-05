"""Utilitaires de test : application jetable et client HTTP minimal.

La suite n'utilise que `unittest` : elle tourne avec `python -m unittest`
(sans rien installer) comme sous `pytest` en intégration continue.
"""

from __future__ import annotations

import base64
import shutil
import sys
import tempfile
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kalendra import security
from kalendra.app import Kalendra
from kalendra.config import Config
from kalendra.db import Database
from kalendra.http import Request

# Le coût PBKDF2 de production (~100 ms) rendrait la suite inutilement lente ;
# l'algorithme testé reste le même, seul le facteur de travail change.
security.PBKDF2_ITERATIONS = 1_000

EVENT_TEMPLATE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Kalendra//tests//FR
BEGIN:VEVENT
UID:{uid}
DTSTAMP:20260101T090000Z
DTSTART:{start}
DTEND:{end}
SUMMARY:{summary}
END:VEVENT
END:VCALENDAR
"""


def make_event(
    uid: str = "evt-1",
    start: str = "20260310T090000Z",
    end: str = "20260310T100000Z",
    summary: str = "Revue archi VCU",
    extra: str = "",
) -> bytes:
    text = EVENT_TEMPLATE.format(uid=uid, start=start, end=end, summary=summary)
    if extra:
        text = text.replace("END:VEVENT", f"{extra}\nEND:VEVENT")
    return text.replace("\n", "\r\n").encode("utf-8")


CARD_TEMPLATE = """BEGIN:VCARD
VERSION:3.0
UID:{uid}
FN:{fn}
N:{fn};;;;
EMAIL:{email}
END:VCARD
"""


def make_card(uid: str = "card-1", fn: str = "Jean Dupont", email: str = "jean@example.org") -> bytes:
    return CARD_TEMPLATE.format(uid=uid, fn=fn, email=email).replace("\n", "\r\n").encode("utf-8")


def ts(value: str) -> int:
    moment = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    return int(moment.timestamp())


@dataclass
class Result:
    status: int
    body: bytes
    headers: dict[str, str]

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)


class Client:
    """Invoque directement `Kalendra.dispatch` : rapide, sans socket."""

    def __init__(self, app: Kalendra, username: str | None = None, password: str = "") -> None:
        self.app = app
        self.auth: str | None = None
        if username is not None:
            raw = f"{username}:{password}".encode()
            self.auth = "Basic " + base64.b64encode(raw).decode("ascii")

    def request(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        anonymous: bool = False,
    ) -> Result:
        merged = {key.lower(): value for key, value in (headers or {}).items()}
        if self.auth and not anonymous:
            merged.setdefault("authorization", self.auth)
        target, _, query = path.partition("?")
        response = self.app.dispatch(
            Request(method=method, path=target, query=query, headers=merged, body=body)
        )
        return Result(
            response.status,
            response.body,
            {name.lower(): value for name, value in response.headers},
        )

    def propfind(self, path: str, body: str, depth: str = "0") -> Result:
        return self.request(
            "PROPFIND",
            path,
            body.encode("utf-8"),
            {"Depth": depth, "Content-Type": "application/xml"},
        )

    def report(self, path: str, body: str, depth: str = "1") -> Result:
        return self.request(
            "REPORT",
            path,
            body.encode("utf-8"),
            {"Depth": depth, "Content-Type": "application/xml"},
        )

    def put(self, path: str, body: bytes, headers: dict[str, str] | None = None) -> Result:
        merged = {"Content-Type": "text/calendar; charset=utf-8"}
        merged.update(headers or {})
        return self.request("PUT", path, body, merged)


class ServerTestCase(unittest.TestCase):
    """Base commune : une application neuve, deux comptes, deux agendas."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="kalendra-test-")
        self.config = Config()
        self.config.db_path = str(Path(self.tmp) / "kalendra.db")
        self.config.bootstrap_admin = ""
        self.config.bootstrap_password = ""
        self.config.base_path = ""
        self.config.public_url = "http://localhost:5232"

        self.db = Database(self.config.db_path)
        self.db.setup()
        self.app = Kalendra(self.config, self.db)

        self.will_id = self.db.create_user(
            "will", "s3cret", email="will@example.org", is_admin=True
        )
        self.perso_id = self.db.create_calendar(self.will_id, "perso", display_name="Personnel")
        self.alice_id = self.db.create_user("alice", "hunter2")
        self.db.create_calendar(self.alice_id, "boulot", display_name="Boulot")

        self.client = Client(self.app, "will", "s3cret")
        self.alice = Client(self.app, "alice", "hunter2")
        self.anon = Client(self.app)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------- assertions

    def assertStatus(self, result: Result, expected: int) -> None:
        self.assertEqual(
            result.status,
            expected,
            msg=f"corps de la réponse : {result.text[:800]}",
        )
