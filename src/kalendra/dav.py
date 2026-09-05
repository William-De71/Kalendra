"""Implémentation des méthodes WebDAV (RFC 4918) et CalDAV (RFC 4791, 6578)."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from xml.etree import ElementTree as ET

from . import __version__
from .db import http_date
from .http import Request, Response, error, text_response, xml_response
from .ics import (
    InvalidCalendarData,
    ical_utc,
    overlaps_range,
    parse_object,
    parse_range_value,
    text_matches,
)
from .props import PropContext, parse_sync_token, resolve_props, sync_token
from .resources import Kind, Resource, children, object_path, resolve
from .security import etag_for
from .vcard import InvalidCardData, card_matches, parse_vcard
from .xmlutil import (
    NS_CALDAV,
    NS_CARDDAV,
    NS_DAV,
    STATUS_NOT_FOUND,
    STATUS_OK,
    caldav,
    carddav,
    dav,
    multistatus,
    parse_xml,
    propstat,
    response_node,
    to_bytes,
)

PRODID = f"-//Kalendra//Kalendra {__version__}//FR"

# `addressbook` doit figurer ici : un client CardDAV qui ne le voit pas dans
# la réponse OPTIONS conclut que le serveur ne gère pas les contacts.
DAV_COMPLIANCE = "1, 2, 3, access-control, calendar-access, addressbook, extended-mkcol"

ALLOW_COLLECTION = "OPTIONS, GET, HEAD, PROPFIND, PROPPATCH, REPORT, MKCALENDAR, MKCOL, DELETE"
ALLOW_OBJECT = "OPTIONS, GET, HEAD, PUT, DELETE, PROPFIND, PROPPATCH, REPORT"

SAFE_HREF = re.compile(r"^[A-Za-z0-9._@%+~-]{1,255}$")


# --------------------------------------------------------------------- filtres


@dataclass(slots=True)
class PropFilter:
    name: str
    is_not_defined: bool = False
    text: str | None = None
    negate: bool = False


@dataclass(slots=True)
class CompFilter:
    name: str
    is_not_defined: bool = False
    time_range: tuple[int | None, int | None] | None = None
    comps: list[CompFilter] = field(default_factory=list)
    props: list[PropFilter] = field(default_factory=list)


def _parse_comp_filter(node: ET.Element) -> CompFilter:
    comp = CompFilter(name=(node.get("name") or "").upper())
    for child in node:
        tag = child.tag
        if tag == caldav("is-not-defined"):
            comp.is_not_defined = True
        elif tag == caldav("time-range"):
            comp.time_range = (
                parse_range_value(child.get("start")),
                parse_range_value(child.get("end")),
            )
        elif tag == caldav("comp-filter"):
            comp.comps.append(_parse_comp_filter(child))
        elif tag == caldav("prop-filter"):
            pf = PropFilter(name=(child.get("name") or "").upper())
            for sub in child:
                if sub.tag == caldav("is-not-defined"):
                    pf.is_not_defined = True
                elif sub.tag == caldav("text-match"):
                    pf.text = (sub.text or "").strip()
                    pf.negate = (sub.get("negate-condition") or "no").lower() == "yes"
            comp.props.append(pf)
    return comp


def _matches(row, comp: CompFilter) -> bool:
    """Évalue un `comp-filter` de composant (VEVENT, VTODO…) sur une ligne stockée."""
    if comp.is_not_defined:
        return row["component"] != comp.name
    if comp.name and row["component"] != comp.name:
        return False
    if comp.time_range and not overlaps_range(row["data"], *comp.time_range):
        return False
    for prop in comp.props:
        if prop.is_not_defined:
            if text_matches(row["data"], comp.name, prop.name, "", negate=False):
                return False
        elif prop.text is not None and not text_matches(
            row["data"], comp.name, prop.name, prop.text, negate=prop.negate
        ):
            return False
    # ex. VEVENT > VALARM : non indexé, on laisse passer
    return all(
        not (sub.is_not_defined and row["component"] == sub.name) for sub in comp.comps
    )


def filter_objects(db, calendar_id: int, root: CompFilter | None) -> list:
    """Applique un filtre CalDAV, avec pré-sélection SQL puis affinage."""
    if root is None or root.name != "VCALENDAR":
        return db.list_objects(calendar_id)

    targets = [c for c in root.comps if not c.is_not_defined]
    if not targets:
        rows = db.list_objects(calendar_id)
        return [row for row in rows if all(_matches(row, c) for c in root.comps)]

    names = sorted({c.name for c in targets if c.name})
    ranges = [c.time_range for c in targets if c.time_range]
    start = min((r[0] for r in ranges if r[0] is not None), default=None) if ranges else None
    end = max((r[1] for r in ranges if r[1] is not None), default=None) if ranges else None
    if root.time_range:
        start = root.time_range[0] if start is None else start
        end = root.time_range[1] if end is None else end

    candidates = db.query_objects(calendar_id, components=names or None, start=start, end=end)
    # Filtres frères au même niveau : sémantique OU (un objet n'a qu'un type).
    return [row for row in candidates if any(_matches(row, c) for c in targets)]


# ------------------------------------------------------------------ requêtes


def _requested_props(body_root: ET.Element | None) -> tuple[list[str], bool, bool]:
    """Renvoie (liste de noms qualifiés, allprop, propname)."""
    if body_root is None:
        return [], True, False
    prop_node = body_root.find(dav("prop"))
    if prop_node is not None:
        return [child.tag for child in prop_node], False, False
    if body_root.find(dav("allprop")) is not None:
        return [], True, False
    if body_root.find(dav("propname")) is not None:
        return [], False, True
    return [], True, False


def _fill_response(
    node: ET.Element, resource: Resource, ctx: PropContext, requested: list[str], allprop: bool
) -> None:
    from .props import ALLPROP

    names = list(requested) if not allprop else list(ALLPROP)
    found, missing = resolve_props(resource, ctx, names)
    propstat(node, found, STATUS_OK)
    if not allprop:
        propstat(node, missing, STATUS_NOT_FOUND)


class DavHandler:
    """Traite une requête déjà authentifiée sur l'arbre CalDAV."""

    def __init__(self, db, config, user) -> None:
        self.db = db
        self.config = config
        self.user = user
        self.ctx = PropContext(db=db, config=config, user=user, base_path=config.base_path)

    # ------------------------------------------------------------- utilitaires

    def href(self, path: str) -> str:
        return self.ctx.href(path)

    def _authorized(self, resource: Resource) -> bool:
        if self.user is None:
            return False
        if resource.user is None:
            return True
        return bool(self.user["is_admin"]) or resource.user["id"] == self.user["id"]

    # ------------------------------------------------------------- répartition

    def handle(self, request: Request, segments: list[str]) -> Response:
        resource = resolve(self.db, segments, request.path)
        method = request.method.upper()

        if method == "OPTIONS":
            return self.options(resource)
        if not self._authorized(resource):
            return error(403, "Accès refusé à cette ressource.")

        handlers = {
            "PROPFIND": self.propfind,
            "PROPPATCH": self.proppatch,
            "REPORT": self.report,
            "GET": self.get,
            "HEAD": self.get,
            "PUT": self.put,
            "DELETE": self.delete,
            "MKCALENDAR": self.mkcalendar,
            "MKCOL": self.mkcol,
        }
        handler = handlers.get(method)
        if handler is None:
            return error(405, f"Méthode {method} non supportée.").header(
                "Allow", ALLOW_COLLECTION if resource.is_collection else ALLOW_OBJECT
            )
        return handler(request, resource)

    # --------------------------------------------------------------- méthodes

    def options(self, resource: Resource) -> Response:
        response = Response(204)
        response.header("DAV", DAV_COMPLIANCE)
        response.header(
            "Allow", ALLOW_COLLECTION if resource.is_collection else ALLOW_OBJECT
        )
        response.header("MS-Author-Via", "DAV")
        return response

    def propfind(self, request: Request, resource: Resource) -> Response:
        if not resource.exists:
            return error(404, "Ressource inconnue.")
        try:
            root = parse_xml(request.body)
        except ValueError as exc:
            return error(400, str(exc))
        requested, allprop, propname = _requested_props(root)

        targets = [resource]
        if request.depth in {"1", "infinity"}:
            targets.extend(children(self.db, resource))

        ms = multistatus()
        for target in targets:
            node = response_node(self.href(target.path))
            if propname:
                from .props import HANDLERS

                available = [
                    ET.Element(name)
                    for name, handler in HANDLERS.items()
                    if handler(target, self.ctx) is not None
                ]
                propstat(node, available, STATUS_OK)
            else:
                _fill_response(node, target, self.ctx, requested, allprop)
            ms.append(node)
        return xml_response(207, to_bytes(ms))

    def proppatch(self, request: Request, resource: Resource) -> Response:
        if resource.kind != Kind.CALENDAR or resource.calendar is None:
            return error(403, "Propriétés modifiables uniquement sur un agenda.")
        try:
            root = parse_xml(request.body)
        except ValueError as exc:
            return error(400, str(exc))
        if root is None:
            return error(400, "Corps propertyupdate manquant.")

        writable = {
            dav("displayname"): "display_name",
            caldav("calendar-description"): "description",
            caldav("calendar-timezone"): "timezone",
            "{http://apple.com/ns/ical/}calendar-color": "color",
            "{http://apple.com/ns/ical/}calendar-order": "sort_order",
        }

        updates: dict[str, object] = {}
        ok: list[ET.Element] = []
        forbidden: list[ET.Element] = []

        for action in root:
            removing = action.tag == dav("remove")
            prop_node = action.find(dav("prop"))
            if prop_node is None:
                continue
            for child in prop_node:
                column = writable.get(child.tag)
                if column is None:
                    forbidden.append(ET.Element(child.tag))
                    continue
                if removing:
                    updates[column] = 0 if column == "sort_order" else ""
                elif column == "sort_order":
                    try:
                        updates[column] = int((child.text or "0").strip())
                    except ValueError:
                        updates[column] = 0
                else:
                    value = child.text or ""
                    if child.tag.endswith("calendar-color"):
                        value = value.strip()[:9]
                    updates[column] = value
                ok.append(ET.Element(child.tag))

        if updates:
            self.db.update_calendar(resource.calendar["id"], **updates)

        ms = multistatus()
        node = response_node(self.href(resource.path))
        propstat(node, ok, STATUS_OK)
        propstat(node, forbidden, "HTTP/1.1 403 Forbidden")
        ms.append(node)
        return xml_response(207, to_bytes(ms))

    def get(self, request: Request, resource: Resource) -> Response:
        if resource.kind == Kind.OBJECT and resource.obj is not None:
            body = resource.obj["data"].encode("utf-8")
            response = Response(200, b"" if request.method == "HEAD" else body)
            type_mime = (
                "text/vcard; charset=utf-8"
                if resource.collection_kind == "addressbook"
                else "text/calendar; charset=utf-8"
            )
            response.header("Content-Type", type_mime)
            response.header("Content-Length", str(len(body)))
            response.header("ETag", resource.obj["etag"])
            response.header("Last-Modified", http_date(resource.obj["updated_at"]))
            return response
        if not resource.exists:
            return error(404, "Ressource inconnue.")
        return error(405, "Utilisez PROPFIND ou REPORT sur une collection.").header(
            "Allow", ALLOW_COLLECTION
        )

    def put(self, request: Request, resource: Resource) -> Response:
        if resource.kind not in {Kind.OBJECT, Kind.MISSING_OBJECT} or resource.calendar is None:
            return error(403, "PUT n'est autorisé que sur une ressource de collection.")
        if not SAFE_HREF.match(resource.name):
            return error(400, "Nom de ressource invalide.")

        carnet = resource.collection_kind == "addressbook"
        attendu = "text/vcard" if carnet else "text/calendar"
        acceptes = (
            {"text/vcard", "text/x-vcard", "application/octet-stream", ""}
            if carnet
            else {"text/calendar", "application/octet-stream", ""}
        )
        content_type = request.header("content-type", attendu).split(";")[0].strip()
        if content_type and content_type not in acceptes:
            return error(415, f"Type {content_type} non supporté ; attendu {attendu}.")

        if len(request.body) > self.config.max_resource_size:
            return error(413, "Ressource trop volumineuse.")

        calendar_id = resource.calendar["id"]
        existing = resource.obj
        if_match = request.header("if-match").strip()
        if_none_match = request.header("if-none-match").strip()

        if if_none_match == "*" and existing is not None:
            return error(412, "La ressource existe déjà.")
        if if_match:
            if existing is None:
                return error(412, "La ressource n'existe pas.")
            candidates = {tag.strip() for tag in if_match.split(",")}
            if if_match != "*" and existing["etag"] not in candidates:
                return error(412, "ETag différent : la ressource a changé côté serveur.")

        data = request.body.decode("utf-8", "replace")

        if carnet:
            try:
                card = parse_vcard(data)
            except InvalidCardData as exc:
                return self._precondition(carddav("valid-address-data"), str(exc))
            # Un UID absent est toléré à l'analyse ; ici on lui substitue le nom
            # de la ressource, seul identifiant dont le serveur soit sûr.
            uid = card.uid or resource.name
            composant, resume = "VCARD", card.fn
            debut = fin = None
            recurrent = False
        else:
            try:
                meta = parse_object(data)
            except InvalidCalendarData as exc:
                return self._precondition(caldav("valid-calendar-data"), str(exc))

            allowed = {c.strip().upper() for c in str(resource.calendar["components"]).split(",")}
            if meta.component not in allowed:
                return self._precondition(
                    caldav("supported-calendar-component"),
                    f"{meta.component} n'est pas accepté par cet agenda.",
                )
            uid = meta.uid
            composant, resume = meta.component, meta.summary
            debut, fin = meta.start, meta.end
            recurrent = meta.recurring

        clash = self.db.get_object_by_uid(calendar_id, uid)
        if clash is not None and clash["href"] != resource.name:
            return self._precondition(
                carddav("no-uid-conflict") if carnet else caldav("no-uid-conflict"),
                f"UID {uid} déjà présent dans cette collection.",
            )

        etag = etag_for(data)
        self.db.put_object(
            calendar_id,
            resource.name,
            data,
            uid=uid,
            component=composant,
            dtstart=debut,
            dtend=fin,
            recurring=recurrent,
            summary=resume,
            etag=etag,
        )
        response = Response(204 if existing is not None else 201)
        response.header("ETag", etag)
        response.header("Content-Length", "0")
        return response

    def delete(self, request: Request, resource: Resource) -> Response:
        if resource.kind == Kind.OBJECT and resource.obj is not None:
            if_match = request.header("if-match").strip()
            if if_match and if_match != "*" and resource.obj["etag"] not in {
                tag.strip() for tag in if_match.split(",")
            }:
                return error(412, "ETag différent.")
            self.db.delete_object(resource.calendar["id"], resource.name)
            return Response(204).header("Content-Length", "0")
        if resource.kind == Kind.CALENDAR and resource.calendar is not None:
            self.db.delete_calendar(resource.calendar["id"])
            return Response(204).header("Content-Length", "0")
        if not resource.exists:
            return error(404, "Ressource inconnue.")
        return error(403, "Suppression non autorisée sur cette ressource.")

    def mkcalendar(self, request: Request, resource: Resource) -> Response:
        carnet = resource.collection_kind == "addressbook"
        quoi = "carnet" if carnet else "agenda"
        if resource.kind == Kind.CALENDAR:
            return error(405, f"Ce {quoi} existe déjà.")
        if resource.kind != Kind.MISSING_CALENDAR or resource.user is None:
            return error(403, "Chemin invalide pour MKCALENDAR.")
        if not SAFE_HREF.match(resource.name):
            return error(400, f"Nom de {quoi} invalide.")
        try:
            root = parse_xml(request.body)
        except ValueError as exc:
            return error(400, str(exc))

        options = self._collection_options(root)
        try:
            self.db.create_calendar(
                resource.user["id"],
                resource.name,
                display_name=options.get("display_name", resource.name),
                description=options.get("description", ""),
                color=options.get("color", "#3584e4"),
                components=options.get("components", "VCARD" if carnet else "VEVENT,VTODO"),
                timezone_ics=options.get("timezone", ""),
                kind="addressbook" if carnet else "calendar",
            )
        except sqlite3.IntegrityError:
            # Nom déjà pris pour ce compte et ce type : c'est un conflit, pas
            # une panne du serveur.
            return error(409, f"Un {quoi} « {resource.name} » existe déjà.")
        return Response(201).header("Content-Length", "0")

    def mkcol(self, request: Request, resource: Resource) -> Response:
        try:
            root = parse_xml(request.body)
        except ValueError as exc:
            return error(400, str(exc))
        if root is not None:
            is_calendar = any(
                node.tag == caldav("calendar")
                for node in root.iter()
                if node.tag.startswith(f"{{{NS_CALDAV}}}")
            )
            is_addressbook = any(
                node.tag == carddav("addressbook")
                for node in root.iter()
                if node.tag.startswith(f"{{{NS_CARDDAV}}}")
            )
            if (
                not is_calendar
                and not is_addressbook
                and root.find(f".//{{{NS_DAV}}}resourcetype") is not None
            ):
                return error(
                    403, "Seuls les agendas et les carnets d'adresses sont supportés."
                )
        return self.mkcalendar(request, resource)

    @staticmethod
    def _collection_options(root: ET.Element | None) -> dict[str, str]:
        options: dict[str, str] = {}
        if root is None:
            return options
        for prop in root.iter(dav("prop")):
            for child in prop:
                if child.tag == dav("displayname"):
                    options["display_name"] = child.text or ""
                elif child.tag in (
                    caldav("calendar-description"),
                    carddav("addressbook-description"),
                ):
                    options["description"] = child.text or ""
                elif child.tag == caldav("calendar-timezone"):
                    options["timezone"] = child.text or ""
                elif child.tag.endswith("calendar-color"):
                    options["color"] = (child.text or "#3584e4").strip()[:9]
                elif child.tag == caldav("supported-calendar-component-set"):
                    comps = [c.get("name", "").upper() for c in child]
                    comps = [c for c in comps if c]
                    if comps:
                        options["components"] = ",".join(comps)
        return options

    @staticmethod
    def _precondition(tag: str, message: str) -> Response:
        root = ET.Element(dav("error"))
        ET.SubElement(root, tag)
        detail = ET.SubElement(root, dav("responsedescription"))
        detail.text = message
        return xml_response(403, to_bytes(root))

    # ---------------------------------------------------------------- REPORT

    def report(self, request: Request, resource: Resource) -> Response:
        try:
            root = parse_xml(request.body)
        except ValueError as exc:
            return error(400, str(exc))
        if root is None:
            return error(400, "Corps de REPORT manquant.")

        if root.tag == caldav("calendar-query"):
            return self._calendar_query(request, resource, root)
        if root.tag == caldav("calendar-multiget"):
            return self._calendar_multiget(request, resource, root)
        if root.tag == dav("sync-collection"):
            return self._sync_collection(request, resource, root)
        if root.tag == caldav("free-busy-query"):
            return self._free_busy(request, resource, root)
        if root.tag == dav("principal-property-search"):
            return self._principal_search(request, resource, root)
        if root.tag == carddav("addressbook-query"):
            return self._addressbook_query(request, resource, root)
        if root.tag == carddav("addressbook-multiget"):
            # Même traitement que son équivalent calendrier : le rapport ne fait
            # qu'énumérer des href, sans rien interpréter du contenu.
            return self._calendar_multiget(request, resource, root)
        return error(501, f"REPORT {root.tag} non implémenté.")

    def _calendar_scope(self, resource: Resource) -> list[Resource]:
        """Agendas concernés : un seul, ou tous ceux du home-set."""
        if resource.kind == Kind.CALENDAR:
            return [resource]
        if resource.kind == Kind.HOME:
            return children(self.db, resource)
        return []

    def _calendar_query(
        self, request: Request, resource: Resource, root: ET.Element
    ) -> Response:
        scope = self._calendar_scope(resource)
        if not scope:
            return error(403, "calendar-query nécessite une collection calendrier.")
        requested, allprop, _ = _requested_props(root)

        filter_node = root.find(caldav("filter"))
        comp_node = filter_node.find(caldav("comp-filter")) if filter_node is not None else None
        comp_filter = _parse_comp_filter(comp_node) if comp_node is not None else None

        ms = multistatus()
        for calendar_resource in scope:
            calendar = calendar_resource.calendar
            for row in filter_objects(self.db, calendar["id"], comp_filter):
                child = Resource(
                    Kind.OBJECT,
                    f"{calendar_resource.path}{row['href']}",
                    user=calendar_resource.user,
                    calendar=calendar,
                    obj=row,
                    name=row["href"],
                )
                node = response_node(self.href(child.path))
                _fill_response(node, child, self.ctx, requested, allprop)
                ms.append(node)
        return xml_response(207, to_bytes(ms))

    def _calendar_multiget(
        self, request: Request, resource: Resource, root: ET.Element
    ) -> Response:
        requested, allprop, _ = _requested_props(root)
        ms = multistatus()
        for href_node in root.findall(dav("href")):
            raw = (href_node.text or "").strip()
            child = self._resource_from_href(raw)
            node = response_node(raw)
            if child is None or child.kind != Kind.OBJECT or not self._authorized(child):
                status = ET.SubElement(node, dav("status"))
                status.text = "HTTP/1.1 404 Not Found"
            else:
                _fill_response(node, child, self.ctx, requested, allprop)
            ms.append(node)
        return xml_response(207, to_bytes(ms))

    def _addressbook_query(
        self, request: Request, resource: Resource, root: ET.Element
    ) -> Response:
        """REPORT `addressbook-query` (RFC 6352 §8.6).

        Le filtre n'est pas traduit en SQL : on applique `card_matches` sur les
        cartes de la collection. Comme côté calendrier, en cas de doute on
        renvoie la carte plutôt que de la masquer.
        """
        if resource.kind != Kind.CALENDAR or resource.calendar is None:
            return error(403, "addressbook-query nécessite un carnet d'adresses.")
        if resource.collection_kind != "addressbook":
            return error(403, "Cette collection n'est pas un carnet d'adresses.")

        requested, allprop, _ = _requested_props(root)
        besoin = ""
        for node in root.iter(carddav("text-match")):
            besoin = (node.text or "").strip()
            if besoin:
                break

        ms = multistatus()
        for row in self.db.list_objects(resource.calendar["id"]):
            if besoin:
                try:
                    card = parse_vcard(row["data"])
                except InvalidCardData:
                    # Carte illisible : on la renvoie plutôt que de la perdre.
                    pass
                else:
                    if not card_matches(card, besoin):
                        continue
            child = Resource(
                Kind.OBJECT,
                object_path(
                    resource.user["username"],
                    resource.calendar["name"],
                    row["href"],
                    "addressbook",
                ),
                user=resource.user,
                calendar=resource.calendar,
                obj=row,
                name=row["href"],
                collection_kind="addressbook",
            )
            node = response_node(child.path)
            _fill_response(node, child, self.ctx, requested, allprop)
            ms.append(node)
        return xml_response(207, to_bytes(ms))

    def _sync_collection(
        self, request: Request, resource: Resource, root: ET.Element
    ) -> Response:
        if resource.kind != Kind.CALENDAR or resource.calendar is None:
            return error(403, "sync-collection nécessite une collection calendrier.")
        calendar = resource.calendar
        requested, allprop, _ = _requested_props(root)

        token_node = root.find(dav("sync-token"))
        token = (token_node.text or "").strip() if token_node is not None else ""
        since = parse_sync_token(token, calendar)
        if since is None:
            root_error = ET.Element(dav("error"))
            ET.SubElement(root_error, dav("valid-sync-token"))
            return xml_response(403, to_bytes(root_error))

        ms = multistatus()
        if since == 0:
            rows = self.db.list_objects(calendar["id"])
            for row in rows:
                child = Resource(
                    Kind.OBJECT,
                    f"{resource.path}{row['href']}",
                    user=resource.user,
                    calendar=calendar,
                    obj=row,
                    name=row["href"],
                )
                node = response_node(self.href(child.path))
                _fill_response(node, child, self.ctx, requested, allprop)
                ms.append(node)
        else:
            for change in self.db.changes_since(calendar["id"], since):
                path = f"{resource.path}{change['href']}"
                node = response_node(self.href(path))
                if change["deleted"]:
                    status = ET.SubElement(node, dav("status"))
                    status.text = "HTTP/1.1 404 Not Found"
                else:
                    row = self.db.get_object(calendar["id"], change["href"])
                    if row is None:
                        status = ET.SubElement(node, dav("status"))
                        status.text = "HTTP/1.1 404 Not Found"
                    else:
                        child = Resource(
                            Kind.OBJECT,
                            path,
                            user=resource.user,
                            calendar=calendar,
                            obj=row,
                            name=row["href"],
                        )
                        _fill_response(node, child, self.ctx, requested, allprop)
                ms.append(node)

        fresh = self.db.get_calendar_by_id(calendar["id"]) or calendar
        token_element = ET.SubElement(ms, dav("sync-token"))
        token_element.text = sync_token(fresh)
        return xml_response(207, to_bytes(ms))

    def _free_busy(self, request: Request, resource: Resource, root: ET.Element) -> Response:
        if resource.kind != Kind.CALENDAR or resource.calendar is None:
            return error(403, "free-busy-query nécessite une collection calendrier.")
        range_node = root.find(caldav("time-range"))
        start = parse_range_value(range_node.get("start")) if range_node is not None else None
        end = parse_range_value(range_node.get("end")) if range_node is not None else None

        rows = self.db.query_objects(
            resource.calendar["id"], components=["VEVENT"], start=start, end=end
        )
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            f"PRODID:{PRODID}",
            "BEGIN:VFREEBUSY",
            f"DTSTAMP:{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}",
        ]
        if start is not None:
            lines.append(f"DTSTART:{ical_utc(start)}")
        if end is not None:
            lines.append(f"DTEND:{ical_utc(end)}")
        for row in rows:
            if row["dtstart"] is None:
                continue
            busy_end = row["dtend"] if row["dtend"] is not None else row["dtstart"] + 3600
            lines.append(f"FREEBUSY;FBTYPE=BUSY:{ical_utc(row['dtstart'])}/{ical_utc(busy_end)}")
        lines += ["END:VFREEBUSY", "END:VCALENDAR", ""]
        return text_response(200, "\r\n".join(lines), "text/calendar; charset=utf-8")

    def _principal_search(
        self, request: Request, resource: Resource, root: ET.Element
    ) -> Response:
        """Recherche minimale : renvoie le principal courant s'il correspond."""
        requested, allprop, _ = _requested_props(root)
        ms = multistatus()
        if self.user is not None:
            principal = Resource(
                Kind.PRINCIPAL,
                f"/principals/{self.user['username']}/",
                user=self.user,
            )
            node = response_node(self.href(principal.path))
            _fill_response(node, principal, self.ctx, requested, allprop)
            ms.append(node)
        return xml_response(207, to_bytes(ms))

    def _resource_from_href(self, href: str) -> Resource | None:
        from urllib.parse import unquote, urlparse

        path = urlparse(href).path
        if self.config.base_path and path.startswith(self.config.base_path):
            path = path[len(self.config.base_path) :]
        segments = [unquote(part) for part in path.strip("/").split("/") if part]
        if not segments:
            return None
        return resolve(self.db, segments, path)
