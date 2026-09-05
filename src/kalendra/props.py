"""Résolution des propriétés WebDAV/CalDAV pour PROPFIND et REPORT."""

from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree as ET

from .db import http_date
from .http import href_quote
from .resources import Kind, Resource, home_path, principal_path
from .xmlutil import caldav, cs, dav, element, ical

PRIVILEGES = ("read", "write", "write-properties", "write-content", "bind", "unbind", "read-acl")

REPORTS = (
    caldav("calendar-query"),
    caldav("calendar-multiget"),
    caldav("free-busy-query"),
    dav("sync-collection"),
    dav("principal-property-search"),
    dav("expand-property"),
)


@dataclass(slots=True)
class PropContext:
    db: object
    config: object
    user: object | None
    base_path: str = ""

    def href(self, path: str) -> str:
        return f"{self.base_path}{path}" if self.base_path else path


def _resourcetype(resource: Resource, ctx: PropContext) -> ET.Element:
    node = element(dav("resourcetype"))
    if resource.kind in {Kind.ROOT, Kind.PRINCIPALS, Kind.HOME}:
        ET.SubElement(node, dav("collection"))
    elif resource.kind == Kind.PRINCIPAL:
        ET.SubElement(node, dav("collection"))
        ET.SubElement(node, dav("principal"))
    elif resource.kind == Kind.CALENDAR:
        ET.SubElement(node, dav("collection"))
        ET.SubElement(node, caldav("calendar"))
    return node


def _displayname(resource: Resource, ctx: PropContext) -> ET.Element | None:
    if resource.kind == Kind.ROOT:
        return element(dav("displayname"), "Kalendra")
    if resource.kind == Kind.PRINCIPALS:
        return element(dav("displayname"), "Principals")
    if resource.kind == Kind.PRINCIPAL and resource.user is not None:
        return element(dav("displayname"), resource.user["display_name"] or resource.user["username"])
    if resource.kind == Kind.HOME and resource.user is not None:
        return element(dav("displayname"), f"Agendas de {resource.user['username']}")
    if resource.kind == Kind.CALENDAR and resource.calendar is not None:
        return element(
            dav("displayname"), resource.calendar["display_name"] or resource.calendar["name"]
        )
    return None


def _current_user_principal(resource: Resource, ctx: PropContext) -> ET.Element:
    node = element(dav("current-user-principal"))
    if ctx.user is None:
        ET.SubElement(node, dav("unauthenticated"))
    else:
        href = ET.SubElement(node, dav("href"))
        href.text = ctx.href(principal_path(ctx.user["username"]))
    return node


def _principal_url(resource: Resource, ctx: PropContext) -> ET.Element | None:
    if resource.kind != Kind.PRINCIPAL or resource.user is None:
        return None
    node = element(dav("principal-URL"))
    href = ET.SubElement(node, dav("href"))
    href.text = ctx.href(principal_path(resource.user["username"]))
    return node


def _principal_collection_set(resource: Resource, ctx: PropContext) -> ET.Element:
    node = element(dav("principal-collection-set"))
    href = ET.SubElement(node, dav("href"))
    href.text = ctx.href("/principals/")
    return node


def _owner(resource: Resource, ctx: PropContext) -> ET.Element | None:
    if resource.user is None:
        return None
    node = element(dav("owner"))
    href = ET.SubElement(node, dav("href"))
    href.text = ctx.href(principal_path(resource.user["username"]))
    return node


def _calendar_home_set(resource: Resource, ctx: PropContext) -> ET.Element | None:
    user = resource.user if resource.kind == Kind.PRINCIPAL else ctx.user
    if user is None:
        return None
    node = element(caldav("calendar-home-set"))
    href = ET.SubElement(node, dav("href"))
    href.text = ctx.href(home_path(user["username"]))
    return node


def _calendar_user_address_set(resource: Resource, ctx: PropContext) -> ET.Element | None:
    user = resource.user if resource.kind == Kind.PRINCIPAL else ctx.user
    if user is None:
        return None
    node = element(caldav("calendar-user-address-set"))
    if user["email"]:
        href = ET.SubElement(node, dav("href"))
        href.text = f"mailto:{user['email']}"
    href = ET.SubElement(node, dav("href"))
    href.text = ctx.href(principal_path(user["username"]))
    return node


def _supported_report_set(resource: Resource, ctx: PropContext) -> ET.Element:
    node = element(dav("supported-report-set"))
    for report in REPORTS:
        supported = ET.SubElement(node, dav("supported-report"))
        wrapper = ET.SubElement(supported, dav("report"))
        ET.SubElement(wrapper, report)
    return node


def _current_user_privilege_set(resource: Resource, ctx: PropContext) -> ET.Element:
    node = element(dav("current-user-privilege-set"))
    for name in PRIVILEGES:
        privilege = ET.SubElement(node, dav("privilege"))
        ET.SubElement(privilege, dav(name))
    return node


def _supported_calendar_component_set(resource: Resource, ctx: PropContext) -> ET.Element | None:
    if resource.calendar is None or resource.kind != Kind.CALENDAR:
        return None
    node = element(caldav("supported-calendar-component-set"))
    for comp in str(resource.calendar["components"]).split(","):
        comp = comp.strip().upper()
        if comp:
            ET.SubElement(node, caldav("comp"), {"name": comp})
    return node


def _supported_calendar_data(resource: Resource, ctx: PropContext) -> ET.Element | None:
    if resource.kind != Kind.CALENDAR:
        return None
    node = element(caldav("supported-calendar-data"))
    ET.SubElement(
        node,
        caldav("calendar-data"),
        {"content-type": "text/calendar", "version": "2.0"},
    )
    return node


def _calendar_description(resource: Resource, ctx: PropContext) -> ET.Element | None:
    if resource.calendar is None or resource.kind != Kind.CALENDAR:
        return None
    return element(caldav("calendar-description"), resource.calendar["description"] or "")


def _calendar_timezone(resource: Resource, ctx: PropContext) -> ET.Element | None:
    if resource.calendar is None or resource.kind != Kind.CALENDAR:
        return None
    value = resource.calendar["timezone"]
    if not value:
        return None
    return element(caldav("calendar-timezone"), value)


def _calendar_color(resource: Resource, ctx: PropContext) -> ET.Element | None:
    if resource.calendar is None or resource.kind != Kind.CALENDAR:
        return None
    return element(ical("calendar-color"), resource.calendar["color"] or "#3584e4")


def _calendar_order(resource: Resource, ctx: PropContext) -> ET.Element | None:
    if resource.calendar is None or resource.kind != Kind.CALENDAR:
        return None
    return element(ical("calendar-order"), str(resource.calendar["sort_order"]))


def _getctag(resource: Resource, ctx: PropContext) -> ET.Element | None:
    if resource.calendar is None or resource.kind != Kind.CALENDAR:
        return None
    return element(cs("getctag"), sync_token(resource.calendar))


def _sync_token(resource: Resource, ctx: PropContext) -> ET.Element | None:
    if resource.calendar is None or resource.kind != Kind.CALENDAR:
        return None
    return element(dav("sync-token"), sync_token(resource.calendar))


def _getetag(resource: Resource, ctx: PropContext) -> ET.Element | None:
    if resource.kind == Kind.OBJECT and resource.obj is not None:
        return element(dav("getetag"), resource.obj["etag"])
    if resource.kind == Kind.CALENDAR and resource.calendar is not None:
        return element(dav("getetag"), f'"{sync_token(resource.calendar)}"')
    return None


def _getcontenttype(resource: Resource, ctx: PropContext) -> ET.Element | None:
    if resource.kind == Kind.OBJECT:
        return element(dav("getcontenttype"), "text/calendar; charset=utf-8; component=vevent")
    return None


def _getcontentlength(resource: Resource, ctx: PropContext) -> ET.Element | None:
    if resource.kind == Kind.OBJECT and resource.obj is not None:
        return element(dav("getcontentlength"), str(resource.obj["size"]))
    return None


def _getlastmodified(resource: Resource, ctx: PropContext) -> ET.Element | None:
    if resource.kind == Kind.OBJECT and resource.obj is not None:
        return element(dav("getlastmodified"), http_date(resource.obj["updated_at"]))
    if resource.kind == Kind.CALENDAR and resource.calendar is not None:
        return element(dav("getlastmodified"), http_date(resource.calendar["created_at"]))
    return None


def _max_resource_size(resource: Resource, ctx: PropContext) -> ET.Element | None:
    if resource.kind != Kind.CALENDAR:
        return None
    return element(caldav("max-resource-size"), str(ctx.config.max_resource_size))


def _calendar_data(resource: Resource, ctx: PropContext) -> ET.Element | None:
    if resource.kind == Kind.OBJECT and resource.obj is not None:
        return element(caldav("calendar-data"), resource.obj["data"])
    return None


def _quota_used(resource: Resource, ctx: PropContext) -> ET.Element | None:
    if resource.kind != Kind.CALENDAR or resource.calendar is None:
        return None
    row = ctx.db.conn.execute(
        "SELECT COALESCE(SUM(size), 0) AS total FROM objects WHERE calendar_id = ?",
        (resource.calendar["id"],),
    ).fetchone()
    return element(dav("quota-used-bytes"), str(row["total"]))


def sync_token(calendar) -> str:
    """Jeton opaque de synchronisation, monotone par agenda."""
    return f"urn:kalendra:sync:{calendar['id']}:{calendar['sync_rev']}"


def parse_sync_token(token: str, calendar) -> int | None:
    """Renvoie la révision contenue dans un jeton, ou None s'il est invalide."""
    if not token:
        return 0
    prefix = f"urn:kalendra:sync:{calendar['id']}:"
    if not token.startswith(prefix):
        return None
    try:
        return int(token[len(prefix) :])
    except ValueError:
        return None


HANDLERS = {
    dav("resourcetype"): _resourcetype,
    dav("displayname"): _displayname,
    dav("current-user-principal"): _current_user_principal,
    dav("principal-URL"): _principal_url,
    dav("principal-collection-set"): _principal_collection_set,
    dav("owner"): _owner,
    dav("supported-report-set"): _supported_report_set,
    dav("current-user-privilege-set"): _current_user_privilege_set,
    dav("getetag"): _getetag,
    dav("getcontenttype"): _getcontenttype,
    dav("getcontentlength"): _getcontentlength,
    dav("getlastmodified"): _getlastmodified,
    dav("sync-token"): _sync_token,
    dav("quota-used-bytes"): _quota_used,
    caldav("calendar-home-set"): _calendar_home_set,
    caldav("calendar-user-address-set"): _calendar_user_address_set,
    caldav("supported-calendar-component-set"): _supported_calendar_component_set,
    caldav("supported-calendar-data"): _supported_calendar_data,
    caldav("calendar-description"): _calendar_description,
    caldav("calendar-timezone"): _calendar_timezone,
    caldav("max-resource-size"): _max_resource_size,
    caldav("calendar-data"): _calendar_data,
    cs("getctag"): _getctag,
    ical("calendar-color"): _calendar_color,
    ical("calendar-order"): _calendar_order,
}

#: Propriétés renvoyées pour `<D:allprop>` (RFC 4918 : les propriétés
#: coûteuses ou spécifiques sont volontairement omises).
ALLPROP = (
    dav("resourcetype"),
    dav("displayname"),
    dav("getetag"),
    dav("getcontenttype"),
    dav("getcontentlength"),
    dav("getlastmodified"),
    dav("owner"),
    dav("current-user-principal"),
    cs("getctag"),
    dav("sync-token"),
    caldav("calendar-description"),
    ical("calendar-color"),
)


def resolve_props(
    resource: Resource, ctx: PropContext, requested: list[str]
) -> tuple[list[ET.Element], list[ET.Element]]:
    """Renvoie (propriétés trouvées, propriétés absentes) pour une ressource."""
    found: list[ET.Element] = []
    missing: list[ET.Element] = []
    for qname in requested:
        handler = HANDLERS.get(qname)
        node = handler(resource, ctx) if handler else None
        if node is None:
            missing.append(ET.Element(qname))
        else:
            found.append(node)
    return found, missing

