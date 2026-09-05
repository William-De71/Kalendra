"""Aides XML pour WebDAV/CalDAV : espaces de noms, sérialisation, analyse sûre."""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

#: Toute déclaration DTD est refusée : cela neutralise à la fois les entités
#: externes (XXE) et l'expansion récursive d'entités (« billion laughs »),
#: sans dépendre d'une bibliothèque tierce.
_DOCTYPE = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.IGNORECASE)

NS_DAV = "DAV:"
NS_CALDAV = "urn:ietf:params:xml:ns:caldav"
NS_CARDDAV = "urn:ietf:params:xml:ns:carddav"
NS_CS = "http://calendarserver.org/ns/"
NS_ICAL = "http://apple.com/ns/ical/"

ET.register_namespace("D", NS_DAV)
ET.register_namespace("C", NS_CALDAV)
ET.register_namespace("CR", NS_CARDDAV)
ET.register_namespace("CS", NS_CS)
ET.register_namespace("IC", NS_ICAL)


def dav(tag: str) -> str:
    return f"{{{NS_DAV}}}{tag}"


def caldav(tag: str) -> str:
    return f"{{{NS_CALDAV}}}{tag}"


def carddav(tag: str) -> str:
    return f"{{{NS_CARDDAV}}}{tag}"


def cs(tag: str) -> str:
    return f"{{{NS_CS}}}{tag}"


def ical(tag: str) -> str:
    return f"{{{NS_ICAL}}}{tag}"


def element(tag: str, text: str | None = None, **attrib: str) -> ET.Element:
    node = ET.Element(tag, {k.replace("_", "-"): v for k, v in attrib.items()})
    if text is not None:
        node.text = text
    return node


def parse_xml(body: bytes) -> ET.Element | None:
    """Analyse un corps XML en refusant les entités externes. None si vide."""
    if not body or not body.strip():
        return None
    if _DOCTYPE.search(body):
        raise ValueError("les déclarations DTD ne sont pas acceptées")
    try:
        return ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError(f"XML invalide : {exc}") from exc


def to_bytes(root: ET.Element) -> bytes:
    """Sérialise un arbre avec déclaration XML."""
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def multistatus() -> ET.Element:
    return ET.Element(dav("multistatus"))


def response_node(href: str) -> ET.Element:
    node = ET.Element(dav("response"))
    href_node = ET.SubElement(node, dav("href"))
    href_node.text = href
    return node


def propstat(parent: ET.Element, props: list[ET.Element], status: str) -> None:
    if not props:
        return
    node = ET.SubElement(parent, dav("propstat"))
    prop = ET.SubElement(node, dav("prop"))
    for child in props:
        prop.append(child)
    status_node = ET.SubElement(node, dav("status"))
    status_node.text = status


STATUS_OK = "HTTP/1.1 200 OK"
STATUS_NOT_FOUND = "HTTP/1.1 404 Not Found"
