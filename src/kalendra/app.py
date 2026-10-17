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
from .http import Request, Response, error, for_log, text_response
from .security import csrf_token, verify_password

logger = logging.getLogger("kalendra")
access_logger = logging.getLogger("kalendra.access")

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
        self._entries: dict[str, tuple[float, int, int | None]] = {}
        self._lock = threading.Lock()

    def _key(self, secret: str, username: str, password: str) -> str:
        payload = f"{username}\0{password}".encode()
        return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    def get(self, secret: str, username: str, password: str) -> tuple[int, int | None] | None:
        """Return (user id, app password id or None) for fresh credentials."""
        if self.ttl <= 0:
            return None
        key = self._key(secret, username, password)
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires, user_id, app_id = entry
            if expires < now:
                self._entries.pop(key, None)
                return None
            return user_id, app_id

    def store(
        self, secret: str, username: str, password: str, user_id: int, app_id: int | None = None
    ) -> None:
        if self.ttl <= 0:
            return
        key = self._key(secret, username, password)
        with self._lock:
            if len(self._entries) >= self.capacity:
                self._entries.clear()
            self._entries[key] = (time.monotonic() + self.ttl, user_id, app_id)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class _LoginThrottle:
    """Refuse an address for a while after too many failed logins.

    PBKDF2 already slows each guess to ~100 ms, which still allows close to a
    million guesses a day from a single client. Counting failures per address
    turns that into a handful per lockout window.

    Keyed on the address alone, never on the account: locking an account
    after N failures would let anyone lock out a user they know the name of.
    Requests without credentials are not failures — every CalDAV client
    starts with one to receive the 401 challenge.
    """

    def __init__(self, max_failures: int, window: int, capacity: int = 4096) -> None:
        self.max_failures = max_failures
        self.window = window
        self.capacity = capacity
        # address -> (failures, start of the counting window, locked until)
        self._entries: dict[str, tuple[int, float, float]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.max_failures > 0 and self.window > 0

    def retry_after(self, address: str) -> int:
        """Seconds the address must still wait, 0 when it may try."""
        if not self.enabled:
            return 0
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(address)
            if entry is None or entry[2] <= now:
                return 0
            return max(1, int(entry[2] - now + 0.999))

    def failure(self, address: str) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        with self._lock:
            failures, started, locked = self._entries.get(address, (0, now, 0.0))
            if now - started > self.window:
                failures, started = 0, now
            failures += 1
            if failures >= self.max_failures:
                locked = now + self.window
            if len(self._entries) >= self.capacity and address not in self._entries:
                self._purge(now)
            self._entries[address] = (failures, started, locked)

    def success(self, address: str) -> None:
        with self._lock:
            self._entries.pop(address, None)

    def _purge(self, now: float) -> None:
        stale = [
            key
            for key, (_, started, locked) in self._entries.items()
            if locked <= now and now - started > self.window
        ]
        for key in stale:
            del self._entries[key]
        if len(self._entries) >= self.capacity:
            # Flooded with distinct addresses: forgetting is better than
            # growing without bound, and the PBKDF2 cost remains.
            self._entries.clear()

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


def client_ip(request: Request, trust_proxy: bool) -> str:
    """Address to charge failed logins to.

    Behind a reverse proxy every request comes from the proxy, which would make
    one attacker lock out everybody; hence `KALENDRA_TRUST_PROXY`. The
    rightmost X-Forwarded-For entry is the one our own proxy appended — the
    ones to its left are whatever the client chose to send.
    """
    if trust_proxy:
        forwarded = request.header("x-forwarded-for")
        if forwarded:
            last = forwarded.split(",")[-1].strip()
            if last:
                return last
    return request.remote_addr


def _loggable_path(path: str, base: str) -> str:
    """The feed token is the feed's only secret: it must not reach the logs."""
    relative = path[len(base) :] if base and path.startswith(base) else path
    if relative.startswith("/feed/"):
        return f"{base}/feed/…"
    return path


class Kalendra:
    """CalDAV application: DAV, public ICS feeds and the admin interface."""

    def __init__(self, config: Config | None = None, database: Database | None = None) -> None:
        self.config = config or Config.from_env()
        self.db = database or Database(self.config.db_path)
        self.db.setup()
        self.auth_cache = _AuthCache(self.config.auth_cache_ttl)
        self.throttle = _LoginThrottle(
            self.config.auth_max_failures, self.config.auth_lockout_seconds
        )
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
        """Route a request, then write its access-log line."""
        started = time.perf_counter()
        method, path = request.method, request.path
        status = 500
        try:
            response = self._route(request)
            status = response.status
            return response
        finally:
            if self.config.access_log:
                self._log_access(request, method, path, status, started)

    def _log_access(
        self, request: Request, method: str, path: str, status: int, started: float
    ) -> None:
        # key=value pairs: greppable as is, and parsed without configuration by
        # Loki, Vector or `jq -R`. Health probes go to debug — one every 30 s
        # would drown everything else.
        user = request.user["username"] if request.user is not None else "-"
        level = logging.DEBUG if path.endswith(("/health", "/healthz")) else logging.INFO
        access_logger.log(
            level,
            "ip=%s method=%s path=%s status=%d ms=%.1f user=%s",
            for_log(client_ip(request, self.config.trust_proxy) or "-"),
            for_log(method),
            for_log(_loggable_path(path, self.config.base_path)),
            status,
            (time.perf_counter() - started) * 1000,
            for_log(user),
        )

    def _route(self, request: Request) -> Response:
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

        address = client_ip(request, self.config.trust_proxy)
        user = self._authenticate(request, address)
        if user is _THROTTLED:
            wait = self.throttle.retry_after(address)
            return error(
                429, "Trop d'échecs d'authentification : réessayez plus tard."
            ).header("Retry-After", str(wait))
        if user is None:
            return self._challenge()
        request.user = user

        # An app password is meant for one CalDAV/CardDAV client: should it
        # leak from a phone, it must not open the web pages that manage the
        # account — app passwords and calendar deletion included.
        if request.via_app_password and segments and segments[0] in {"view", "admin"}:
            return error(
                401, "Un mot de passe d'application ne donne pas accès à l'interface web."
            ).header("WWW-Authenticate", REALM)

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

    def _authenticate(self, request: Request, address: str = ""):
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
            # Checked before the lockout: a cache hit proves the password was
            # right a moment ago, so a family member behind the same address as
            # an attacker keeps working.
            user_id, app_id = cached
            user = self.db.get_user_by_id(user_id)
            # A revoked app password must stop working now, not when the cache
            # entry expires: one indexed lookup is cheap next to PBKDF2.
            revoked = app_id is not None and self.db.get_app_password(app_id) is None
            if user is not None and user["username"] == username and not revoked:
                request.via_app_password = app_id is not None
                return user

        # Refused before PBKDF2, so a locked-out client no longer costs CPU.
        if self.throttle.retry_after(address):
            return _THROTTLED

        user = self.db.get_user(username)
        if user is None:
            verify_password(password, DUMMY_HASH)
            self.throttle.failure(address)
            return None

        # App passwords first: a SHA-256 lookup, where the account password
        # costs a full PBKDF2 round. A device configured with one therefore
        # authenticates faster, and never tries the account password.
        app = self.db.find_app_password(int(user["id"]), password)
        if app is not None:
            self.throttle.success(address)
            self.db.touch_app_password(int(app["id"]))
            self.auth_cache.store(secret, username, password, int(user["id"]), int(app["id"]))
            request.via_app_password = True
            return user

        if not verify_password(password, user["password_hash"]):
            self.throttle.failure(address)
            return None
        self.throttle.success(address)
        self.auth_cache.store(secret, username, password, int(user["id"]))
        return user

    @staticmethod
    def _challenge() -> Response:
        return error(401, "Authentification requise.").header("WWW-Authenticate", REALM)


#: Sentinel distinct from None ("no valid credentials"): the caller answers 429
#: rather than 401, which would invite the client to try again at once.
_THROTTLED = object()


def _wants_html(request: Request) -> bool:
    return "text/html" in request.header("accept")
