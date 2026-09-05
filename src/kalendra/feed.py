"""Flux ICS publics en lecture seule.

Google Calendar et Proton Calendar ne savent pas se connecter à un serveur
CalDAV tiers : ils ne consomment qu'une URL iCalendar en lecture seule. Ce
module expose donc chaque agenda sous ``/feed/<jeton>.ics``, protégé par un
jeton aléatoire révocable plutôt que par une authentification HTTP (ces
services ne présentent aucun identifiant).
"""

from __future__ import annotations

from .http import Request, Response, error
from .ics import build_feed
from .security import etag_for

PRODID = "-//Kalendra//Flux ICS//FR"


def handle_feed(db, config, request: Request, token: str) -> Response:
    if not config.feeds_enabled:
        return error(404, "Les flux ICS sont désactivés sur ce serveur.")

    token = token[:-4] if token.endswith(".ics") else token
    calendar = db.get_calendar_by_token(token)
    if calendar is None:
        return error(404, "Flux inconnu ou révoqué.")

    rows = db.list_objects(calendar["id"])
    payload = build_feed(
        name=calendar["display_name"] or calendar["name"],
        description=calendar["description"],
        color=calendar["color"],
        objects=[row["data"] for row in rows],
        refresh_minutes=config.feed_refresh_minutes,
        prodid=PRODID,
    )
    body = payload.encode("utf-8")
    etag = etag_for(body)

    if request.header("if-none-match").strip() == etag:
        return Response(304, b"", [("ETag", etag)])

    response = Response(200, b"" if request.method == "HEAD" else body)
    response.header("Content-Type", "text/calendar; charset=utf-8")
    response.header("Content-Length", str(len(body)))
    response.header("ETag", etag)
    response.header("Cache-Control", f"public, max-age={config.feed_refresh_minutes * 60}")
    response.header(
        "Content-Disposition", f'inline; filename="{calendar["name"]}.ics"'
    )
    return response
