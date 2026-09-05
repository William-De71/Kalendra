"""Interface web d'administration (inspirée de Baïkal, en beaucoup plus petit).

Protégée par l'authentification HTTP Basic d'un compte `is_admin`. Comme le
navigateur rejoue automatiquement les identifiants Basic, chaque formulaire
porte un jeton anti-CSRF dérivé du secret stocké en base.
"""

from __future__ import annotations

from html import escape
from urllib.parse import parse_qs, quote

from . import __version__
from .http import Request, Response, error, text_response
from .security import csrf_valid

STYLE = """
:root { color-scheme: light dark; --bg:#fbfbfd; --fg:#16161d; --muted:#606070;
  --line:#dcdce4; --card:#fff; --accent:#3054c8; --danger:#b3261e; }
@media (prefers-color-scheme: dark) { :root { --bg:#15151a; --fg:#e9e9ef;
  --muted:#a0a0b0; --line:#2c2c36; --card:#1d1d24; --accent:#8aa6ff; --danger:#f2836b; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.55 ui-sans-serif,
  system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
header { padding:20px 24px; border-bottom:1px solid var(--line); display:flex;
  align-items:baseline; gap:12px; flex-wrap:wrap; }
header h1 { margin:0; font-size:19px; letter-spacing:-.01em; }
header span { color:var(--muted); font-size:13px; }
main { max-width:1040px; margin:0 auto; padding:24px; }
section { background:var(--card); border:1px solid var(--line); border-radius:12px;
  padding:18px 20px; margin-bottom:22px; }
h2 { font-size:15px; margin:0 0 14px; text-transform:uppercase; letter-spacing:.06em;
  color:var(--muted); }
table { width:100%; border-collapse:collapse; font-size:14px; }
th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line);
  vertical-align:middle; }
th { color:var(--muted); font-weight:600; font-size:12px; text-transform:uppercase; }
tr:last-child td { border-bottom:none; }
code { font:12.5px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; word-break:break-all; }
input, select { font:inherit; padding:7px 9px; border:1px solid var(--line); border-radius:8px;
  background:var(--bg); color:var(--fg); min-width:0; }
button { font:inherit; padding:7px 13px; border-radius:8px; border:1px solid transparent;
  background:var(--accent); color:#fff; cursor:pointer; }
button.ghost { background:transparent; border-color:var(--line); color:var(--fg); }
button.danger { background:transparent; border-color:var(--line); color:var(--danger); }
form.inline { display:inline; }
form.grid { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-top:14px; }
.flash { padding:10px 14px; border-radius:10px; margin-bottom:18px; border:1px solid var(--line);
  background:var(--card); }
.muted { color:var(--muted); }
.badge { display:inline-block; padding:1px 8px; border-radius:999px; border:1px solid var(--line);
  font-size:12px; color:var(--muted); }
footer { color:var(--muted); font-size:12.5px; padding:0 24px 32px; max-width:1040px;
  margin:0 auto; }
"""


def _page(title: str, body: str) -> Response:
    html = (
        "<!doctype html><html lang=fr><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title><style>{STYLE}</style></head><body>{body}</body></html>"
    )
    return text_response(200, html, "text/html; charset=utf-8")


def _form_data(request: Request) -> dict[str, str]:
    parsed = parse_qs(request.text, keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items()}


def _base_url(config, request: Request) -> str:
    if config.public_url:
        return config.public_url
    host = request.header("x-forwarded-host") or request.header("host") or "localhost"
    scheme = request.header("x-forwarded-proto") or "http"
    return f"{scheme}://{host}{config.base_path}"


def handle_admin(db, config, request: Request, segments: list[str], token: str) -> Response:
    """Point d'entrée de l'UI admin ; `segments` exclut le préfixe `admin`."""
    if not config.admin_ui:
        return error(404, "Interface d'administration désactivée.")

    if request.method in {"GET", "HEAD"}:
        return _dashboard(db, config, request, token)

    if request.method != "POST":
        return error(405, "Méthode non supportée.").header("Allow", "GET, POST")

    form = _form_data(request)
    if not csrf_valid(db.secret_key(), request.user["username"], form.get("csrf", "")):
        return error(403, "Jeton CSRF invalide.")

    action = "/".join(segments)
    try:
        message = _apply(db, form, action)
    except ValueError as exc:
        message = f"Erreur : {exc}"
    except Exception as exc:  # noqa: BLE001
        message = f"Erreur : {exc}"

    location = f"{config.base_path}/admin?msg={quote(message)}"
    return Response(303, b"", [("Location", location), ("Content-Length", "0")])


def _apply(db, form: dict[str, str], action: str) -> str:
    if action == "users/create":
        username = form.get("username", "").strip()
        password = form.get("password", "")
        if not username or not password:
            raise ValueError("identifiant et mot de passe requis")
        db.create_user(
            username,
            password,
            display_name=form.get("display_name", "").strip(),
            email=form.get("email", "").strip(),
            is_admin=form.get("is_admin") == "on",
        )
        return f"Utilisateur « {username} » créé."

    if action == "users/password":
        user_id = int(form["user_id"])
        password = form.get("password", "")
        if not password:
            raise ValueError("mot de passe vide")
        db.set_password(user_id, password)
        return "Mot de passe mis à jour."

    if action == "users/delete":
        user_id = int(form["user_id"])
        if db.count_users() <= 1:
            raise ValueError("impossible de supprimer le dernier compte")
        db.delete_user(user_id)
        return "Utilisateur supprimé."

    if action == "calendars/create":
        user_id = int(form["user_id"])
        name = form.get("name", "").strip()
        db.create_calendar(
            user_id,
            name,
            display_name=form.get("display_name", "").strip() or name,
            description=form.get("description", "").strip(),
            color=form.get("color", "#3584e4"),
        )
        return f"Agenda « {name} » créé."

    if action == "calendars/delete":
        db.delete_calendar(int(form["calendar_id"]))
        return "Agenda supprimé."

    if action == "calendars/token":
        db.rotate_feed_token(int(form["calendar_id"]))
        return "Jeton de flux régénéré : l'ancienne URL ne fonctionne plus."

    if action == "calendars/feed":
        calendar_id = int(form["calendar_id"])
        calendar = db.get_calendar_by_id(calendar_id)
        if calendar is None:
            raise ValueError("agenda inconnu")
        db.update_calendar(calendar_id, feed_enabled=0 if calendar["feed_enabled"] else 1)
        return "État du flux ICS modifié."

    raise ValueError(f"action inconnue : {action}")


def _dashboard(db, config, request: Request, token: str) -> Response:
    base = _base_url(config, request)
    message = request.query_param("msg")
    users = db.list_users()

    parts = [
        "<header><h1>Kalendra</h1>"
        f"<span>v{escape(__version__)} · administration</span>"
        f"<span class=muted>connecté : {escape(request.user['username'])}</span></header><main>"
    ]
    if message:
        parts.append(f"<div class=flash>{escape(message)}</div>")

    parts.append(
        "<section><h2>Raccordement des clients</h2>"
        "<table><tr><th>Client</th><th>Type</th><th>URL</th></tr>"
        f"<tr><td>Evolution / Thunderbird / DAVx5 / iOS</td><td>CalDAV lecture-écriture</td>"
        f"<td><code>{escape(base)}/</code></td></tr>"
        f"<tr><td>Google Calendar · Proton Calendar</td><td>Abonnement ICS lecture seule</td>"
        f"<td class=muted>URL par agenda, voir ci-dessous</td></tr></table></section>"
    )

    for user in users:
        calendars = db.list_calendars(user["id"])
        admin_badge = " <span class=badge>admin</span>" if user["is_admin"] else ""
        rows = []
        for calendar in calendars:
            count = db.calendar_stats(calendar["id"])
            feed = (
                f"<code>{escape(base)}/feed/{escape(calendar['feed_token'] or '')}.ics</code>"
                if calendar["feed_enabled"] and calendar["feed_token"]
                else "<span class=muted>désactivé</span>"
            )
            rows.append(
                "<tr>"
                f"<td><strong>{escape(calendar['display_name'] or calendar['name'])}</strong>"
                f"<div class=muted><code>{escape(base)}/calendars/"
                f"{escape(user['username'])}/{escape(calendar['name'])}/</code></div></td>"
                f"<td>{count}</td>"
                f"<td>{feed}</td>"
                f"<td style='white-space:nowrap'>"
                f"{_button(config.base_path, token, 'calendars/token', 'Régénérer', calendar_id=calendar['id'])}"
                f"{_button(config.base_path, token, 'calendars/feed', 'Activer/couper', calendar_id=calendar['id'])}"
                f"{_button(config.base_path, token, 'calendars/delete', 'Supprimer', calendar_id=calendar['id'], cls='danger', confirm=True)}"
                "</td></tr>"
            )
        table = (
            "<table><tr><th>Agenda</th><th>Objets</th><th>Flux ICS</th><th></th></tr>"
            + ("".join(rows) or "<tr><td colspan=4 class=muted>Aucun agenda.</td></tr>")
            + "</table>"
        )
        parts.append(
            f"<section><h2>{escape(user['username'])}{admin_badge}</h2>{table}"
            f"<form class=grid method=post action='{config.base_path}/admin/calendars/create'>"
            f"<input type=hidden name=csrf value='{escape(token)}'>"
            f"<input type=hidden name=user_id value='{user['id']}'>"
            "<input name=name placeholder='identifiant (perso)' required pattern='[A-Za-z0-9._-]+'>"
            "<input name=display_name placeholder='nom affiché'>"
            "<input name=color type=color value='#3584e4'>"
            "<button>Ajouter un agenda</button></form>"
            "<form class=grid method=post "
            f"action='{config.base_path}/admin/users/password'>"
            f"<input type=hidden name=csrf value='{escape(token)}'>"
            f"<input type=hidden name=user_id value='{user['id']}'>"
            "<input name=password type=password placeholder='nouveau mot de passe' required>"
            "<button class=ghost>Changer le mot de passe</button>"
            "</form>"
            f"<form class=inline method=post action='{config.base_path}/admin/users/delete' "
            "onsubmit=\"return confirm('Supprimer ce compte et ses agendas ?')\">"
            f"<input type=hidden name=csrf value='{escape(token)}'>"
            f"<input type=hidden name=user_id value='{user['id']}'>"
            "<button class=danger>Supprimer le compte</button></form>"
            "</section>"
        )

    parts.append(
        "<section><h2>Nouvel utilisateur</h2>"
        f"<form class=grid method=post action='{config.base_path}/admin/users/create'>"
        f"<input type=hidden name=csrf value='{escape(token)}'>"
        "<input name=username placeholder='identifiant' required pattern='[A-Za-z0-9._-]+'>"
        "<input name=display_name placeholder='nom affiché'>"
        "<input name=email type=email placeholder='email (facultatif)'>"
        "<input name=password type=password placeholder='mot de passe' required>"
        "<label class=muted><input type=checkbox name=is_admin> admin</label>"
        "<button>Créer</button></form></section>"
    )

    parts.append(
        "</main><footer>Kalendra — CalDAV en lecture/écriture pour Evolution, Thunderbird, "
        "DAVx5 et iOS ; flux ICS en lecture seule pour Google Calendar et Proton Calendar, "
        "qui ne savent pas parler CalDAV à un serveur tiers.</footer>"
    )
    return _page("Kalendra — administration", "".join(parts))


def _button(
    base_path: str,
    token: str,
    action: str,
    label: str,
    *,
    cls: str = "ghost",
    confirm: bool = False,
    **fields: object,
) -> str:
    hidden = "".join(
        f"<input type=hidden name={escape(key)} value='{escape(str(value))}'>"
        for key, value in fields.items()
    )
    guard = " onsubmit=\"return confirm('Confirmer ?')\"" if confirm else ""
    return (
        f"<form class=inline method=post action='{base_path}/admin/{action}'{guard}>"
        f"<input type=hidden name=csrf value='{escape(token)}'>{hidden}"
        f"<button class='{cls}'>{escape(label)}</button></form> "
    )
