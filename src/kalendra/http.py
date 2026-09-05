"""Objets requête/réponse minimalistes, indépendants de tout framework."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote, unquote

STATUS_TEXT = {
    200: "OK",
    201: "Created",
    204: "No Content",
    207: "Multi-Status",
    301: "Moved Permanently",
    302: "Found",
    303: "See Other",
    304: "Not Modified",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    412: "Precondition Failed",
    413: "Payload Too Large",
    415: "Unsupported Media Type",
    422: "Unprocessable Entity",
    500: "Internal Server Error",
    501: "Not Implemented",
    507: "Insufficient Storage",
}


@dataclass(slots=True)
class Request:
    method: str
    path: str
    query: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    user: Any = None

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    @property
    def depth(self) -> str:
        return self.header("depth", "0").strip().lower()

    def query_param(self, name: str, default: str = "") -> str:
        values = parse_qs(self.query).get(name)
        return values[0] if values else default

    @property
    def segments(self) -> list[str]:
        return [unquote(part) for part in self.path.strip("/").split("/") if part]

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


@dataclass(slots=True)
class Response:
    status: int = 200
    body: bytes = b""
    headers: list[tuple[str, str]] = field(default_factory=list)

    def header(self, name: str, value: str) -> Response:
        self.headers.append((name, value))
        return self

    @property
    def reason(self) -> str:
        return STATUS_TEXT.get(self.status, "Unknown")


def text_response(status: int, message: str = "", content_type: str = "text/plain; charset=utf-8") -> Response:
    body = message.encode("utf-8")
    return Response(status, body, [("Content-Type", content_type)])


def xml_response(status: int, payload: bytes) -> Response:
    return Response(status, payload, [("Content-Type", 'application/xml; charset="utf-8"')])


def error(status: int, message: str = "") -> Response:
    text = message or STATUS_TEXT.get(status, "Error")
    return text_response(status, f"{status} {STATUS_TEXT.get(status, '')}\n{text}\n")


def href_quote(path: str) -> str:
    """Encode un chemin pour l'élément `<D:href>` en gardant les séparateurs."""
    return quote(path, safe="/@:+~")
