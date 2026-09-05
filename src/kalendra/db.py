"""SQLite layer: schema, migrations and data access.

The schema fits in four tables (`users`, `calendars`, `objects`, `changes`)
— `calendars` also holds CardDAV address books, told apart by its `kind`
column, which hands them sync revisions for free — plus a key/value `meta`
table. Sync revisions (`calendars.sync_rev`) feed both `getctag` and the
`sync-token` of the `sync-collection` REPORT.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime

from .security import hash_password, new_token

SCHEMA_VERSION = 3

#: Definition of `calendars`, shared between the initial schema and the v3
#: migration that rebuilds the table to widen its uniqueness constraint.
SCHEMA_CALENDARS = """CREATE TABLE IF NOT EXISTS calendars (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind          TEXT    NOT NULL DEFAULT 'calendar',
    name          TEXT    NOT NULL,
    display_name  TEXT    NOT NULL DEFAULT '',
    description   TEXT    NOT NULL DEFAULT '',
    color         TEXT    NOT NULL DEFAULT '#3584e4',
    sort_order    INTEGER NOT NULL DEFAULT 0,
    timezone      TEXT    NOT NULL DEFAULT '',
    components    TEXT    NOT NULL DEFAULT 'VEVENT,VTODO',
    sync_rev      INTEGER NOT NULL DEFAULT 1,
    feed_token    TEXT    UNIQUE,
    feed_enabled  INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL
);

-- Uniqueness covers (user_id, kind, name) rather than (user_id, name): a
-- "personal" calendar and a "personal" address book are two distinct
-- collections in two different URL trees. An index rather than a table
-- constraint, because SQLite can recreate an index but not alter a constraint.
CREATE UNIQUE INDEX IF NOT EXISTS idx_calendars_nom
    ON calendars (user_id, kind, name);
"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    display_name  TEXT    NOT NULL DEFAULT '',
    email         TEXT    NOT NULL DEFAULT '',
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL
);

{SCHEMA_CALENDARS}

CREATE TABLE IF NOT EXISTS objects (
    id          INTEGER PRIMARY KEY,
    calendar_id INTEGER NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
    href        TEXT    NOT NULL,
    uid         TEXT    NOT NULL,
    etag        TEXT    NOT NULL,
    data        TEXT    NOT NULL,
    component   TEXT    NOT NULL,
    dtstart     INTEGER,
    dtend       INTEGER,
    recurring   INTEGER NOT NULL DEFAULT 0,
    summary     TEXT    NOT NULL DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    UNIQUE (calendar_id, href)
);

CREATE INDEX IF NOT EXISTS idx_objects_range ON objects (calendar_id, dtstart, dtend);
CREATE INDEX IF NOT EXISTS idx_objects_uid   ON objects (calendar_id, uid);

CREATE TABLE IF NOT EXISTS changes (
    id          INTEGER PRIMARY KEY,
    calendar_id INTEGER NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
    href        TEXT    NOT NULL,
    sync_rev    INTEGER NOT NULL,
    deleted     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (calendar_id, href)
);

CREATE INDEX IF NOT EXISTS idx_changes_rev ON changes (calendar_id, sync_rev);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_local = threading.local()


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def http_date(iso: str) -> str:
    """Convertit un horodatage ISO stocké en date HTTP (RFC 7231)."""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        dt = datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")


def connect(path: str) -> sqlite3.Connection:
    """Open a configured connection (WAL, foreign keys, row factory)."""
    if path != ":memory:":
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the schema and apply migrations."""
    # executescript() implicitly commits the running transaction, so this is
    # deliberately not wrapped in `transaction()`.
    conn.executescript(SCHEMA)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 2:
        # v2: address books share the calendar tables, hence a column telling
        # the two apart. Existing databases hold calendars only, and the
        # default value suits them.
        colonnes = {r["name"] for r in conn.execute("PRAGMA table_info(calendars)")}
        if "kind" not in colonnes:
            conn.execute(
                "ALTER TABLE calendars ADD COLUMN kind TEXT NOT NULL DEFAULT 'calendar'"
            )
    if version < 3:
        # v3: uniqueness must include `kind`, otherwise an address book cannot
        # carry the same name as a calendar of the same account. The original
        # constraint lives in the CREATE TABLE, so the table is rebuilt.
        # `executescript(SCHEMA)` has already created the index, so its presence
        # tells us nothing. The reliable signal is the old constraint, still
        # written in the table's SQL until we rebuild it.
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='calendars'"
        ).fetchone()
        ancienne = ddl is not None and "UNIQUE (user_id, name)" in (ddl[0] or "")
        colonnes = [r[1] for r in conn.execute("PRAGMA table_info(calendars)")]
        if colonnes and ancienne:
            noms = ", ".join(colonnes)
            conn.executescript(
                "PRAGMA foreign_keys=off;\n"
                "BEGIN;\n"
                "ALTER TABLE calendars RENAME TO calendars_v2;\n"
                + SCHEMA_CALENDARS
                + f"INSERT INTO calendars ({noms}) SELECT {noms} FROM calendars_v2;\n"
                "DROP TABLE calendars_v2;\n"
                "COMMIT;\n"
                "PRAGMA foreign_keys=on;"
            )

    if version < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    with transaction(conn):
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('secret_key', ?)",
            (new_token(32),),
        )


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Immediate transaction: avoids concurrent write conflicts."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


class Database:
    """Small data store; one connection per thread."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._shared: sqlite3.Connection | None = None
        if path == ":memory:":
            # An in-memory database does not survive a connection change.
            self._shared = connect(path)
            init_db(self._shared)

    # ------------------------------------------------------------------ infra

    @property
    def conn(self) -> sqlite3.Connection:
        if self._shared is not None:
            return self._shared
        conn = getattr(_local, "conn", None)
        if conn is None or getattr(_local, "path", None) != self.path:
            conn = connect(self.path)
            _local.conn = conn
            _local.path = self.path
        return conn

    def setup(self) -> None:
        if self._shared is None:
            init_db(self.conn)

    def secret_key(self) -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'secret_key'").fetchone()
        return row["value"] if row else "kalendra"

    # ------------------------------------------------------------ utilisateurs

    def create_user(
        self,
        username: str,
        password: str,
        *,
        display_name: str = "",
        email: str = "",
        is_admin: bool = False,
    ) -> int:
        username = username.strip()
        if not username or "/" in username:
            raise ValueError("nom d'utilisateur invalide")
        with transaction(self.conn) as conn:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, display_name, email, is_admin,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    username,
                    hash_password(password),
                    display_name or username,
                    email,
                    1 if is_admin else 0,
                    utcnow(),
                ),
            )
            return int(cur.lastrowid)

    def set_password(self, user_id: int, password: str) -> None:
        with transaction(self.conn) as conn:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (hash_password(password), user_id),
            )

    def update_user(self, user_id: int, **fields: object) -> None:
        """Update an account's descriptive fields.

        `username` is deliberately not editable: it appears in the CalDAV URLs
        clients have already stored, and they would lose their calendars.
        The password keeps `set_password`, which must hash it.
        """
        allowed = {"display_name", "email", "is_admin"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{k} = ?" for k in updates)
        with transaction(self.conn) as conn:
            conn.execute(
                f"UPDATE users SET {assignments} WHERE id = ?",
                (*updates.values(), user_id),
            )

    def delete_user(self, user_id: int) -> None:
        with transaction(self.conn) as conn:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def get_user(self, username: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()

    def get_user_by_id(self, user_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    def list_users(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM users ORDER BY username"))

    def count_users(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])

    def count_admins(self) -> int:
        """Used to refuse deleting or demoting the last administrator."""
        return int(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE is_admin = 1"
            ).fetchone()["n"]
        )

    # --------------------------------------------------------------- agendas

    def create_calendar(
        self,
        user_id: int,
        name: str,
        *,
        display_name: str = "",
        description: str = "",
        color: str = "#3584e4",
        components: str = "VEVENT,VTODO",
        timezone_ics: str = "",
        kind: str = "calendar",
    ) -> int:
        name = name.strip()
        if not name or "/" in name:
            raise ValueError("nom d'agenda invalide")
        with transaction(self.conn) as conn:
            cur = conn.execute(
                "INSERT INTO calendars (user_id, kind, name, display_name, description, color,"
                " components, timezone, feed_token, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    kind,
                    name,
                    display_name or name,
                    description,
                    color,
                    components,
                    timezone_ics,
                    new_token(),
                    utcnow(),
                ),
            )
            return int(cur.lastrowid)

    def create_addressbook(self, user_id: int, name: str, **kw: object) -> int:
        """An address book is a collection shaped exactly like a calendar.

        Sharing the table hands it the sync revisions, ETags and change journal
        already proven on the CalDAV side, at no cost.
        """
        kw.pop("kind", None)
        kw.setdefault("components", "VCARD")
        return self.create_calendar(user_id, name, kind="addressbook", **kw)  # type: ignore[arg-type]

    def get_calendar(self, user_id: int, name: str, kind: str = "calendar") -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM calendars WHERE user_id = ? AND name = ? AND kind = ?",
            (user_id, name, kind),
        ).fetchone()

    def get_calendar_by_id(self, calendar_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM calendars WHERE id = ?", (calendar_id,)
        ).fetchone()

    def get_calendar_by_token(self, token: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM calendars WHERE feed_token = ? AND feed_enabled = 1", (token,)
        ).fetchone()

    def list_addressbooks(self, user_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM calendars WHERE user_id = ? AND kind = 'addressbook'"
                " ORDER BY sort_order, name",
                (user_id,),
            )
        )

    def list_all_addressbooks(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT c.*, u.username FROM calendars c JOIN users u ON u.id = c.user_id"
                " WHERE c.kind = 'addressbook' ORDER BY u.username, c.sort_order, c.name"
            )
        )

    def list_calendars(self, user_id: int) -> list[sqlite3.Row]:
        # Filtered on kind: without it, address books would surface everywhere
        # calendars are expected (CalDAV, /view/, dashboard).
        return list(
            self.conn.execute(
                "SELECT * FROM calendars WHERE user_id = ? AND kind = 'calendar'"
                " ORDER BY sort_order, name",
                (user_id,),
            )
        )

    def list_all_calendars(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT c.*, u.username FROM calendars c JOIN users u ON u.id = c.user_id"
                " WHERE c.kind = 'calendar' ORDER BY u.username, c.sort_order, c.name"
            )
        )

    def update_calendar(self, calendar_id: int, **fields: object) -> None:
        allowed = {
            "display_name",
            "description",
            "color",
            "sort_order",
            "timezone",
            "components",
            "feed_enabled",
            "feed_token",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{k} = ?" for k in updates)
        with transaction(self.conn) as conn:
            conn.execute(
                f"UPDATE calendars SET {assignments} WHERE id = ?",
                (*updates.values(), calendar_id),
            )

    def delete_calendar(self, calendar_id: int) -> None:
        with transaction(self.conn) as conn:
            conn.execute("DELETE FROM calendars WHERE id = ?", (calendar_id,))

    def rotate_feed_token(self, calendar_id: int) -> str:
        token = new_token()
        self.update_calendar(calendar_id, feed_token=token)
        return token

    def stats(self) -> dict[str, int]:
        """Dashboard-wide counters, in a handful of aggregates.

        Counting calendar by calendar would mean one query per displayed row;
        here the number of queries does not depend on the number of accounts.
        """
        def _un(sql: str) -> int:
            return int(self.conn.execute(sql).fetchone()["n"])

        return {
            "users": _un("SELECT COUNT(*) AS n FROM users"),
            "admins": _un("SELECT COUNT(*) AS n FROM users WHERE is_admin = 1"),
            "calendars": _un("SELECT COUNT(*) AS n FROM calendars WHERE kind = 'calendar'"),
            "addressbooks": _un("SELECT COUNT(*) AS n FROM calendars WHERE kind = 'addressbook'"),
            "contacts": _un("SELECT COUNT(*) AS n FROM objects WHERE component = 'VCARD'"),
            "feeds": _un(
                "SELECT COUNT(*) AS n FROM calendars"
                " WHERE feed_enabled = 1 AND kind = 'calendar'"
            ),
            "objects": _un("SELECT COUNT(*) AS n FROM objects WHERE component != 'VCARD'"),
            "events": _un("SELECT COUNT(*) AS n FROM objects WHERE component = 'VEVENT'"),
            "todos": _un("SELECT COUNT(*) AS n FROM objects WHERE component = 'VTODO'"),
        }

    def object_counts(self) -> dict[int, int]:
        """Object count per calendar, in a single query."""
        return {
            int(row["calendar_id"]): int(row["n"])
            for row in self.conn.execute(
                "SELECT calendar_id, COUNT(*) AS n FROM objects GROUP BY calendar_id"
            )
        }

    def calendar_stats(self, calendar_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM objects WHERE calendar_id = ?", (calendar_id,)
        ).fetchone()
        return int(row["n"])

    # ---------------------------------------------------------------- objets

    def list_objects(self, calendar_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM objects WHERE calendar_id = ? ORDER BY href", (calendar_id,)
            )
        )

    def get_object(self, calendar_id: int, href: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM objects WHERE calendar_id = ? AND href = ?", (calendar_id, href)
        ).fetchone()

    def get_object_by_uid(self, calendar_id: int, uid: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM objects WHERE calendar_id = ? AND uid = ?", (calendar_id, uid)
        ).fetchone()

    def put_object(
        self,
        calendar_id: int,
        href: str,
        data: str,
        *,
        uid: str,
        component: str,
        dtstart: int | None,
        dtend: int | None,
        recurring: bool,
        summary: str,
        etag: str,
    ) -> int:
        """Insert or replace an object and bump the sync revision."""
        now = utcnow()
        with transaction(self.conn) as conn:
            rev = self._bump_rev(conn, calendar_id)
            existing = conn.execute(
                "SELECT id, created_at FROM objects WHERE calendar_id = ? AND href = ?",
                (calendar_id, href),
            ).fetchone()
            created = existing["created_at"] if existing else now
            conn.execute(
                "INSERT INTO objects (calendar_id, href, uid, etag, data, component, dtstart,"
                " dtend, recurring, summary, size, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (calendar_id, href) DO UPDATE SET"
                " uid = excluded.uid, etag = excluded.etag, data = excluded.data,"
                " component = excluded.component, dtstart = excluded.dtstart,"
                " dtend = excluded.dtend, recurring = excluded.recurring,"
                " summary = excluded.summary, size = excluded.size,"
                " updated_at = excluded.updated_at",
                (
                    calendar_id,
                    href,
                    uid,
                    etag,
                    data,
                    component,
                    dtstart,
                    dtend,
                    1 if recurring else 0,
                    summary,
                    len(data.encode("utf-8")),
                    created,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO changes (calendar_id, href, sync_rev, deleted) VALUES (?, ?, ?, 0)"
                " ON CONFLICT (calendar_id, href) DO UPDATE SET sync_rev = excluded.sync_rev,"
                " deleted = 0",
                (calendar_id, href, rev),
            )
            return rev

    def delete_object(self, calendar_id: int, href: str) -> int:
        with transaction(self.conn) as conn:
            rev = self._bump_rev(conn, calendar_id)
            conn.execute(
                "DELETE FROM objects WHERE calendar_id = ? AND href = ?", (calendar_id, href)
            )
            conn.execute(
                "INSERT INTO changes (calendar_id, href, sync_rev, deleted) VALUES (?, ?, ?, 1)"
                " ON CONFLICT (calendar_id, href) DO UPDATE SET sync_rev = excluded.sync_rev,"
                " deleted = 1",
                (calendar_id, href, rev),
            )
            return rev

    @staticmethod
    def _bump_rev(conn: sqlite3.Connection, calendar_id: int) -> int:
        conn.execute("UPDATE calendars SET sync_rev = sync_rev + 1 WHERE id = ?", (calendar_id,))
        row = conn.execute(
            "SELECT sync_rev FROM calendars WHERE id = ?", (calendar_id,)
        ).fetchone()
        return int(row["sync_rev"]) if row else 1

    def changes_since(self, calendar_id: int, since_rev: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT href, sync_rev, deleted FROM changes"
                " WHERE calendar_id = ? AND sync_rev > ? ORDER BY sync_rev",
                (calendar_id, since_rev),
            )
        )

    def query_objects(
        self,
        calendar_id: int,
        *,
        components: list[str] | None = None,
        start: int | None = None,
        end: int | None = None,
    ) -> list[sqlite3.Row]:
        """SQL pre-filter (component + range). Recurrences are refined in Python."""
        sql = "SELECT * FROM objects WHERE calendar_id = ?"
        params: list[object] = [calendar_id]
        if components:
            placeholders = ", ".join("?" for _ in components)
            sql += f" AND component IN ({placeholders})"
            params.extend(components)
        if start is not None:
            sql += " AND (dtend IS NULL OR dtend > ?)"
            params.append(start)
        if end is not None:
            sql += " AND (dtstart IS NULL OR dtstart < ?)"
            params.append(end)
        sql += " ORDER BY href"
        return list(self.conn.execute(sql, params))
