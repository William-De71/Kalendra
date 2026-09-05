"""WebDAV resource tree and URL resolution.

    /                                     root (discovery)
    /principals/                          principal collection
    /principals/<user>/                   principal
    /calendars/<user>/                    calendar-home-set
    /calendars/<user>/<calendar>/         calendar collection
    /calendars/<user>/<calendar>/<x>.ics  calendar resource
    /addressbooks/<user>/                 addressbook-home-set
    /addressbooks/<user>/<book>/          address book collection
    /addressbooks/<user>/<book>/<x>.vcf   vCard resource

Both trees have the same shape, so address books reuse the same `Kind` values,
told apart by `Resource.collection_kind`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum

from .http import href_quote


class Kind(Enum):
    ROOT = "root"
    PRINCIPALS = "principals"
    PRINCIPAL = "principal"
    HOME = "home"
    CALENDAR = "calendar"
    OBJECT = "object"
    MISSING_CALENDAR = "missing_calendar"
    MISSING_OBJECT = "missing_object"
    NOT_FOUND = "not_found"


@dataclass(slots=True)
class Resource:
    kind: Kind
    path: str
    user: sqlite3.Row | None = None
    calendar: sqlite3.Row | None = None
    obj: sqlite3.Row | None = None
    name: str = ""
    #: "calendar" or "addressbook" — which tree produced this resource.
    collection_kind: str = "calendar"

    @property
    def is_collection(self) -> bool:
        return self.kind in {
            Kind.ROOT,
            Kind.PRINCIPALS,
            Kind.PRINCIPAL,
            Kind.HOME,
            Kind.CALENDAR,
        }

    @property
    def exists(self) -> bool:
        return self.kind not in {Kind.NOT_FOUND, Kind.MISSING_CALENDAR, Kind.MISSING_OBJECT}


def principal_path(username: str) -> str:
    return f"/principals/{href_quote(username)}/"


#: URL root for each collection type.
RACINE = {"calendar": "calendars", "addressbook": "addressbooks"}


def home_path(username: str, kind: str = "calendar") -> str:
    return f"/{RACINE[kind]}/{href_quote(username)}/"


def calendar_path(username: str, calendar_name: str, kind: str = "calendar") -> str:
    return f"/{RACINE[kind]}/{href_quote(username)}/{href_quote(calendar_name)}/"


def object_path(username: str, calendar_name: str, href: str, kind: str = "calendar") -> str:
    return (
        f"/{RACINE[kind]}/{href_quote(username)}/"
        f"{href_quote(calendar_name)}/{href_quote(href)}"
    )


def resolve(db, segments: list[str], path: str) -> Resource:
    """Translate a list of URL segments into a resource."""
    if not segments:
        return Resource(Kind.ROOT, "/")

    root = segments[0]

    if root == "principals":
        if len(segments) == 1:
            return Resource(Kind.PRINCIPALS, "/principals/")
        user = db.get_user(segments[1])
        if user is None or len(segments) > 2:
            return Resource(Kind.NOT_FOUND, path)
        return Resource(Kind.PRINCIPAL, principal_path(user["username"]), user=user)

    if root not in {"calendars", "addressbooks"}:
        return Resource(Kind.NOT_FOUND, path)

    ck = "calendar" if root == "calendars" else "addressbook"

    if len(segments) == 1:
        return Resource(Kind.NOT_FOUND, path)

    user = db.get_user(segments[1])
    if user is None:
        return Resource(Kind.NOT_FOUND, path)

    if len(segments) == 2:
        return Resource(Kind.HOME, home_path(user["username"], ck), user=user, collection_kind=ck)

    calendar = db.get_calendar(user["id"], segments[2], ck)
    if len(segments) == 3:
        if calendar is None:
            return Resource(
                Kind.MISSING_CALENDAR, path, user=user, name=segments[2], collection_kind=ck
            )
        return Resource(
            Kind.CALENDAR,
            calendar_path(user["username"], calendar["name"], ck),
            user=user,
            calendar=calendar,
            name=calendar["name"],
            collection_kind=ck,
        )

    if len(segments) == 4:
        if calendar is None:
            return Resource(Kind.NOT_FOUND, path)
        obj = db.get_object(calendar["id"], segments[3])
        target = object_path(user["username"], calendar["name"], segments[3], ck)
        if obj is None:
            return Resource(
                Kind.MISSING_OBJECT,
                target,
                user=user,
                calendar=calendar,
                name=segments[3],
                collection_kind=ck,
            )
        return Resource(
            Kind.OBJECT,
            target,
            user=user,
            calendar=calendar,
            obj=obj,
            name=segments[3],
            collection_kind=ck,
        )

    return Resource(Kind.NOT_FOUND, path)


def children(db, resource: Resource) -> list[Resource]:
    """Direct children (Depth: 1)."""
    if resource.kind == Kind.ROOT:
        return [Resource(Kind.PRINCIPALS, "/principals/")]
    if resource.kind == Kind.PRINCIPALS:
        return [
            Resource(Kind.PRINCIPAL, principal_path(user["username"]), user=user)
            for user in db.list_users()
        ]
    if resource.kind == Kind.HOME and resource.user is not None:
        ck = resource.collection_kind
        collections = (
            db.list_addressbooks(resource.user["id"])
            if ck == "addressbook"
            else db.list_calendars(resource.user["id"])
        )
        return [
            Resource(
                Kind.CALENDAR,
                calendar_path(resource.user["username"], calendar["name"], ck),
                user=resource.user,
                calendar=calendar,
                name=calendar["name"],
                collection_kind=ck,
            )
            for calendar in collections
        ]
    if resource.kind == Kind.CALENDAR and resource.calendar is not None:
        return [
            Resource(
                Kind.OBJECT,
                object_path(
                    resource.user["username"],
                    resource.calendar["name"],
                    obj["href"],
                    resource.collection_kind,
                ),
                user=resource.user,
                calendar=resource.calendar,
                obj=obj,
                name=obj["href"],
                collection_kind=resource.collection_kind,
            )
            for obj in db.list_objects(resource.calendar["id"])
        ]
    return []
