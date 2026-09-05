"""Adaptateur ASGI optionnel (uvicorn, hypercorn, gunicorn+uvicorn worker).

    uvicorn kalendra.asgi:app --http h11 --host 0.0.0.0 --port 5232

Le paramètre ``--http h11`` est important : l'analyseur httptools refuse les
verbes qu'il ne connaît pas, alors que h11 accepte n'importe quel jeton de
méthode — ce dont CalDAV a besoin (MKCALENDAR, REPORT…).

Le traitement étant synchrone (SQLite), il est délégué au pool de threads
d'asyncio pour ne pas bloquer la boucle d'événements.
"""

from __future__ import annotations

import asyncio
from urllib.parse import quote

from . import __version__
from .app import Kalendra
from .config import Config
from .http import Request, error


class ASGIApplication:
    """Enveloppe ASGI autour de `Kalendra.dispatch`."""

    def __init__(self, application: Kalendra | None = None) -> None:
        self._application = application

    @property
    def application(self) -> Kalendra:
        if self._application is None:
            self._application = Kalendra(Config.from_env())
        return self._application

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            return

        body = bytearray()
        limit = self.application.config.max_request_body
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > limit:
                await self._send(send, error(413, "Corps de requête trop volumineux."))
                return
            if not message.get("more_body", False):
                break

        raw_path = scope.get("raw_path")
        if isinstance(raw_path, bytes):
            path = raw_path.decode("latin-1").split("?", 1)[0]
        else:
            path = quote(scope["path"], safe="/@:+~%")

        request = Request(
            method=scope["method"],
            path=path,
            query=scope.get("query_string", b"").decode("latin-1"),
            headers={
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])
            },
            body=bytes(body),
        )

        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, self.application.dispatch, request)
        await self._send(send, response)

    @staticmethod
    async def _lifespan(receive, send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    @staticmethod
    async def _send(send, response) -> None:
        headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in response.headers]
        if not any(key.lower() == b"content-length" for key, _ in headers):
            headers.append((b"content-length", str(len(response.body)).encode("latin-1")))
        headers.append((b"server", f"Kalendra/{__version__}".encode("latin-1")))
        await send({"type": "http.response.start", "status": response.status, "headers": headers})
        await send({"type": "http.response.body", "body": response.body})


app = ASGIApplication()
