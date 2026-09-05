"""Serveur HTTP de la bibliothèque standard, sans dépendance externe.

`ThreadingHTTPServer` accepte n'importe quel verbe HTTP, ce qui est
indispensable ici : PROPFIND, REPORT, MKCALENDAR et PROPPATCH ne figurent
dans aucune liste blanche de méthodes.

Exposer ce serveur directement sur Internet n'est pas recommandé : placez-le
derrière un reverse proxy TLS (Caddy, nginx, Traefik). En réseau local ou
derrière un tunnel, il tient très largement la charge d'un usage personnel ou
familial.
"""

from __future__ import annotations

import logging
import socket
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .app import Kalendra
from .http import Request, Response, error

logger = logging.getLogger("kalendra")

MAX_BODY = 64 * 1024 * 1024


class _Handler(BaseHTTPRequestHandler):
    """Adaptateur générique : tout verbe HTTP est routé vers `dispatch`."""

    protocol_version = "HTTP/1.1"
    server_version = f"Kalendra/{__version__}"
    sys_version = ""

    application: Kalendra  # injecté par `make_server`

    # BaseHTTPRequestHandler cherche do_<VERBE> ; on intercepte avant.
    def __getattr__(self, name: str):
        if name.startswith("do_"):
            return self._handle
        raise AttributeError(name)

    def _handle(self) -> None:
        try:
            response = self._build_response()
        except Exception:
            logger.exception("erreur interne sur %s %s", self.command, self.path)
            response = error(500, "Erreur interne.")
        self._write(response)

    def _build_response(self) -> Response:
        length = self.headers.get("Content-Length")
        body = b""
        if length:
            try:
                size = int(length)
            except ValueError:
                return error(400, "Content-Length invalide.")
            if size > MAX_BODY:
                return error(413, "Corps de requête trop volumineux.")
            body = self.rfile.read(size) if size > 0 else b""
        elif (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            body = self._read_chunked()

        path, _, query = self.path.partition("?")
        request = Request(
            method=self.command,
            path=path,
            query=query,
            headers={key.lower(): value for key, value in self.headers.items()},
            body=body,
        )
        return self.application.dispatch(request)

    def _read_chunked(self) -> bytes:
        chunks = bytearray()
        while True:
            line = self.rfile.readline(128).strip()
            if not line:
                break
            try:
                size = int(line.split(b";")[0], 16)
            except ValueError:
                break
            if size == 0:
                self.rfile.readline(4)
                break
            chunks.extend(self.rfile.read(size))
            self.rfile.readline(4)
            if len(chunks) > MAX_BODY:
                break
        return bytes(chunks)

    def _write(self, response: Response) -> None:
        body = response.body
        try:
            self.send_response(response.status, response.reason)
            seen = set()
            for name, value in response.headers:
                self.send_header(name, value)
                seen.add(name.lower())
            if "content-length" not in seen:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("client déconnecté pendant l'écriture de la réponse")

    def log_message(self, fmt: str, *args) -> None:  # pragma: no cover - bruit
        logger.info("%s %s", self.address_string(), fmt % args)

    def log_error(self, fmt: str, *args) -> None:  # pragma: no cover - bruit
        logger.warning("%s %s", self.address_string(), fmt % args)


class KalendraServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    address_family = socket.AF_INET

    def __init__(self, address, handler, application: Kalendra) -> None:
        self.application = application
        handler.application = application
        super().__init__(address, handler)


def make_server(application: Kalendra, host: str, port: int) -> KalendraServer:
    if ":" in host:  # adresse IPv6 littérale
        KalendraServer.address_family = socket.AF_INET6
    return KalendraServer((host, port), _Handler, application)


def serve(application: Kalendra, host: str, port: int) -> None:
    """Démarre le serveur et bloque jusqu'à interruption."""
    httpd = make_server(application, host, port)
    logger.info("écoute sur http://%s:%s%s/", host, port, application.config.base_path)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:  # pragma: no cover
        print("\nArrêt demandé.", file=sys.stderr)
    finally:
        httpd.shutdown()
        httpd.server_close()


def serve_in_thread(
    application: Kalendra, host: str = "127.0.0.1", port: int = 0
) -> tuple[socketserver.BaseServer, threading.Thread, int]:
    """Démarre le serveur dans un thread : utilisé par la suite de tests."""
    httpd = make_server(application, host, port)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1})
    thread.daemon = True
    thread.start()
    return httpd, thread, httpd.server_address[1]
