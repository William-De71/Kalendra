"""Application core: authentication and routing, fully synchronous.

`Kalendra.dispatch()` turns a `Request` into a `Response` while knowing nothing
about transport. Two adapters expose it: `kalendra.server` (standard-library
HTTP server, no dependencies at all) and `kalendra.asgi` (uvicorn or gunicorn,
for those who prefer installing them).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from urllib.parse import unquote

from . import __version__
from .admin import handle_admin
from .calendarview import handle_view
from .config import Config
from .dav import DavHandler
from .db import Database
from .feed import handle_feed
from .http import Request, Response, error, text_response
from .security import csrf_token, verify_password

logger = logging.getLogger("kalendra")

REALM = 'Basic realm="Kalendra", charset="UTF-8"'

#: Dummy hash: keeps verification cost identical for a non-existent account,
#: so response timing never reveals which usernames exist.
DUMMY_HASH = "pbkdf2_sha256$240000$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="  # noqa: E501 (empreinte indivisible)


class _AuthCache:
    """Briefly remember verified credentials.

    PBKDF2 deliberately costs ~100 ms, yet a CalDAV client queries the server in
    bursts (PROPFIND, REPORT, multiget and so on). So the HMAC digest of the
    username/password pair is kept for a short while. A password change takes
    effect after that delay at the latest.
    """

    def __init__(self, ttl: int, capacity: int = 512) -> None:
        self.ttl = ttl
        self.capacity = capacity
        self._entries: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def _key(self, secret: str, username: str, password: str) -> str:
        payload = f"{username}\0{password}".encode()
        return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    def get(self, secret: str, username: str, password: str) -> int | None:
        if self.ttl <= 0:
            return None
        key = self._key(secret, username, password)
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires, user_id = entry
            if expires < now:
                self._entries.pop(key, None)
                return None
            return user_id

    def store(self, secret: str, username: str, password: str, user_id: int) -> None:
        if self.ttl <= 0:
            return
        key = self._key(secret, username, password)
        with self._lock:
            if len(self._entries) >= self.capacity:
                self._entries.clear()
            self._entries[key] = (time.monotonic() + self.ttl, user_id)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class Kalendra:
    """CalDAV application: DAV, public ICS feeds and the admin interface."""

    def __init__(self, config: Config | None = None, database: Database | None = None) -> None:
        self.config = config or Config.from_env()
        self.db = database or Database(self.config.db_path)
        self.db.setup()
        self.auth_cache = _AuthCache(self.config.auth_cache_ttl)
        self._bootstrap()

    # ------------------------------------------------------------- démarrage

    def _bootstrap(self) -> None:
        """Create the initial administrator account when the environment asks for it."""
        if not self.config.bootstrap_admin or not self.config.bootstrap_password:
            return
        if self.db.get_user(self.config.bootstrap_admin) is not None:
            return
        user_id = self.db.create_user(
            self.config.bootstrap_admin,
            self.config.bootstrap_password,
            display_name=self.config.bootstrap_admin,
            is_admin=True,
        )
        self.db.create_calendar(user_id, "perso", display_name="Personnel")
        logger.info("compte administrateur « %s » créé", self.config.bootstrap_admin)

    # -------------------------------------------------------------- routage

    def dispatch(self, request: Request) -> Response:
        base = self.config.base_path
        path = request.path
        if base and path.startswith(base):
            path = path[len(base) :] or "/"
        request.path = path

        if path in {"/health", "/healthz"}:
            payload = json.dumps(
                {"status": "ok", "version": __version__, "users": self.db.count_users()}
            )
            return text_response(200, payload, "application/json")

        if path.startswith("/.well-known/carddav"):
            return Response(301, b"", [("Location", f"{base}/"), ("Content-Length", "0")])
        if path.startswith("/.well-known/caldav"):
            return Response(301, b"", [("Location", f"{base}/"), ("Content-Length", "0")])
        if path.startswith("/.well-known/"):
            return error(404, "Service inconnu.")

        segments = [unquote(part) for part in path.strip("/").split("/") if part]

        # ICS feeds sit outside authentication on purpose: Google and Proton
        # present no credentials. The token is the key.
        if segments and segments[0] == "feed":
            token = segments[1] if len(segments) > 1 else ""
            return handle_feed(self.db, self.config, request, token)

        user = self._authenticate(request)
        if user is None:
            return self._challenge()
        request.user = user

        if segments and segments[0] == "view":
            if not self.config.admin_ui:
                return error(404, "Interface web désactivée.")
            # The anti-CSRF token serves the import: the view is read-only
            # everywhere else, but the upload form writes, and the browser
            # replays Basic credentials automatically.
            token = csrf_token(self.db.secret_key(), user["username"])
            return handle_view(self.db, self.config, request, segments[1:], token)

        if segments and segments[0] == "admin":
            if not user["is_admin"]:
                return error(403, "Compte administrateur requis.")
            token = csrf_token(self.db.secret_key(), user["username"])
            return handle_admin(self.db, self.config, request, segments[1:], token)

        if not segments and request.method in {"GET", "HEAD"} and _wants_html(request):
            target = f"{base}/admin" if self.config.admin_ui else f"{base}/calendars/"
            return Response(302, b"", [("Location", target), ("Content-Length", "0")])

        return DavHandler(self.db, self.config, user).handle(request, segments)

    # -------------------------------------------------------- authentification

    def _authenticate(self, request: Request):
        header = request.header("authorization")
        if not header.lower().startswith("basic "):
            return None
        try:
            raw = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        except Exception:
            return None
        username, separator, password = raw.partition(":")
        if not separator:
            return None

        secret = self.db.secret_key()
        cached = self.auth_cache.get(secret, username, password)
        if cached is not None:
            user = self.db.get_user_by_id(cached)
            if user is not None and user["username"] == username:
                return user

        user = self.db.get_user(username)
        if user is None:
            verify_password(password, DUMMY_HASH)
            return None
        if not verify_password(password, user["password_hash"]):
            return None
        self.auth_cache.store(secret, username, password, int(user["id"]))
        return user

    @staticmethod
    def _challenge() -> Response:
        return error(401, "Authentification requise.").header("WWW-Authenticate", REALM)


def _wants_html(request: Request) -> bool:
    return "text/html" in request.header("accept")
