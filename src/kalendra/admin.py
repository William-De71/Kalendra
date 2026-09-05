"""Web admin interface (inspired by Baïkal, on a much smaller scale).

Protected by HTTP Basic authentication of an `is_admin` account. Since the
browser replays Basic credentials automatically, every form carries an
anti-CSRF token derived from the secret stored in the database.
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
.chiffres { display:flex; gap:10px; flex-wrap:wrap; margin-bottom:16px; }
.chiffre { border:1px solid var(--line); border-radius:10px; padding:10px 16px;
  min-width:96px; }
.chiffre b { display:block; font-size:22px; letter-spacing:-.02em; }
.chiffre span { color:var(--muted); font-size:12px; text-transform:uppercase;
  letter-spacing:.05em; }
.etat { color:var(--muted); }
.etat.on { color:#2c9d4a; }
form.recherche { display:flex; gap:8px; margin-bottom:14px; flex-wrap:wrap; }
form.recherche input { flex:1; min-width:180px; }
a.bouton { font:inherit; padding:6px 12px; border-radius:8px; border:1px solid var(--line);
  color:var(--fg); text-decoration:none; display:inline-block; }
details { margin-top:14px; }
details summary { cursor:pointer; color:var(--accent); font-size:14px; }
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
    """Entry point of the admin UI; `segments` excludes the `admin` prefix."""
    if not config.admin_ui:
        return error(404, "Interface d'administration désactivée.")

    if request.method in {"GET", "HEAD"}:
        if len(segments) == 2 and segments[0] == "users" and segments[1].isdigit():
            return _user_page(db, config, request, token, int(segments[1]))
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
    except Exception as exc:
        message = f"Erreur : {exc}"

    # After an action on an account, return to its page rather than the top of
    # the list: otherwise the admin hunts for the row after every change.
    cible = "/admin"
    if action not in {"users/create", "users/delete"} and form.get("user_id", "").isdigit():
        cible = f"/admin/users/{form['user_id']}"
    location = f"{config.base_path}{cible}?msg={quote(message)}"
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

    if action == "users/edit":
        user_id = int(form["user_id"])
        user = db.get_user_by_id(user_id)
        if user is None:
            raise ValueError("compte inconnu")
        is_admin = form.get("is_admin") == "on"
        # Removing one's own admin flag — or the last admin's — would lock the
        # interface out and leave the database unadministrable.
        if user["is_admin"] and not is_admin and db.count_admins() <= 1:
            raise ValueError("il doit rester au moins un administrateur")
        # Only fields actually present in the form are written: a partial POST
        # must not erase an email address it never carried.
        champs: dict[str, object] = {"is_admin": 1 if is_admin else 0}
        if "display_name" in form:
            champs["display_name"] = form["display_name"].strip() or user["username"]
        if "email" in form:
            champs["email"] = form["email"].strip()
        db.update_user(user_id, **champs)
        return "Compte mis à jour."

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
        user = db.get_user_by_id(user_id)
        if user is not None and user["is_admin"] and db.count_admins() <= 1:
            raise ValueError("impossible de supprimer le dernier administrateur")
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

    if action == "addressbooks/create":
        user_id = int(form["user_id"])
        name = form.get("name", "").strip()
        db.create_addressbook(
            user_id,
            name,
            display_name=form.get("display_name", "").strip() or name,
        )
        return f"Carnet « {name} » créé."

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


def _etat(actif: bool, detail: str) -> str:
    """Service status dot, green when active."""
    if actif:
        return f"<span class='etat on'>●</span> {escape(detail)}"
    return "<span class=etat>●</span> <span class=muted>désactivé</span>"


def _dashboard(db, config, request: Request, token: str) -> Response:
    base = _base_url(config, request)
    message = request.query_param("msg")
    users = db.list_users()

    parts = [
        "<header><h1>Kalendra</h1>"
        f"<span>v{escape(__version__)} · administration</span>"
        f"<span class=muted>connecté : {escape(request.user['username'])}</span>"
        f"<a href='{config.base_path}/view/' style='margin-left:auto'>Voir les agendas →</a>"
        "</header><main>"
    ]
    if message:
        parts.append(f"<div class=flash>{escape(message)}</div>")

    st = db.stats()
    parts.append(
        "<section><h2>Vue d'ensemble</h2><div class=chiffres>"
        + "".join(
            f"<div class=chiffre><b>{valeur}</b><span>{escape(etiquette)}</span></div>"
            for etiquette, valeur in (
                ("comptes", st["users"]),
                ("dont admins", st["admins"]),
                ("agendas", st["calendars"]),
                ("carnets", st["addressbooks"]),
                ("objets", st["objects"]),
                ("événements", st["events"]),
                ("tâches", st["todos"]),
                ("contacts", st["contacts"]),
            )
        )
        + "</div><table><tr><th>Service</th><th>État</th></tr>"
        f"<tr><td>CalDAV</td><td>{_etat(True, 'actif')}</td></tr>"
        f"<tr><td>CardDAV</td><td>"
        f"{_etat(True, str(st['addressbooks']) + ' carnet(s)')}</td></tr>"
        f"<tr><td>Flux ICS publics</td><td>"
        f"{_etat(config.feeds_enabled, str(st['feeds']) + ' agenda(s) exposé(s)')}</td></tr>"
        f"<tr><td>Interface web</td><td>{_etat(config.admin_ui, 'active')}</td></tr>"
        "</table></section>"
    )

    parts.append(
        "<section><h2>Raccordement des clients</h2>"
        "<table><tr><th>Client</th><th>Type</th><th>URL</th></tr>"
        f"<tr><td>Evolution / Thunderbird / DAVx5 / iOS</td><td>CalDAV lecture-écriture</td>"
        f"<td><code>{escape(base)}/</code></td></tr>"
        f"<tr><td>Evolution / Thunderbird / DAVx5 / iOS</td><td>CardDAV lecture-écriture</td>"
        f"<td><code>{escape(base)}/addressbooks/</code></td></tr>"
        f"<tr><td>Google Calendar · Proton Calendar</td><td>Abonnement ICS lecture seule</td>"
        f"<td class=muted>URL par agenda, voir ci-dessous</td></tr></table></section>"
    )

    # Server-side filter: no JavaScript, and it also covers accounts the
    # browser has not paged into view yet.
    requete = (request.query_param("q") or "").strip().lower()
    if requete:
        users = [
            u
            for u in users
            if requete in u["username"].lower()
            or requete in (u["display_name"] or "").lower()
            or requete in (u["email"] or "").lower()
        ]

    compteurs = db.object_counts()
    lignes = []
    for user in users:
        calendars = db.list_calendars(user["id"])
        objets = sum(compteurs.get(c["id"], 0) for c in calendars)
        fiche = f"{config.base_path}/admin/users/{user['id']}"
        badge = " <span class=badge>admin</span>" if user["is_admin"] else ""
        noms = ", ".join(escape(c["display_name"] or c["name"]) for c in calendars)
        lignes.append(
            "<tr>"
            f"<td><a href='{fiche}'><strong>{escape(user['username'])}</strong></a>{badge}"
            f"<div class=muted>{escape(user['display_name'] or '')}"
            f"{' · ' + escape(user['email']) if user['email'] else ''}</div></td>"
            f"<td>{len(calendars)}<div class=muted>{noms or '—'}</div></td>"
            f"<td>{objets}</td>"
            f"<td style='white-space:nowrap'><a class=bouton href='{fiche}'>Gérer</a></td>"
            "</tr>"
        )

    vide = (
        "<tr><td colspan=4 class=muted>Aucun compte ne correspond à "
        f"« {escape(requete)} ».</td></tr>"
        if requete
        else "<tr><td colspan=4 class=muted>Aucun compte.</td></tr>"
    )
    parts.append(
        f"<section><h2>Utilisateurs <span class=badge>{len(users)}</span></h2>"
        f"<form class=recherche method=get action='{config.base_path}/admin'>"
        f"<input name=q value='{escape(requete)}' "
        "placeholder='filtrer par identifiant, nom ou email'>"
        "<button class=ghost>Filtrer</button>"
        + (
            f"<a class=bouton href='{config.base_path}/admin'>Tout afficher</a>"
            if requete
            else ""
        )
        + "</form>"
        "<table><tr><th>Compte</th><th>Agendas</th><th>Objets</th><th></th></tr>"
        + ("".join(lignes) or vide)
        + "</table>"
        "<details><summary>Ajouter un utilisateur</summary>"
        f"<form class=grid method=post action='{config.base_path}/admin/users/create'>"
        f"<input type=hidden name=csrf value='{escape(token)}'>"
        "<input name=username placeholder='identifiant' required pattern='[A-Za-z0-9._-]+'>"
        "<input name=display_name placeholder='nom affiché'>"
        "<input name=email type=email placeholder='email (facultatif)'>"
        "<input name=password type=password placeholder='mot de passe' required>"
        "<label class=muted><input type=checkbox name=is_admin> admin</label>"
        "<button>Créer</button></form></details></section>"
    )


    parts.append(
        "</main><footer>Kalendra — CalDAV en lecture/écriture pour Evolution, Thunderbird, "
        "DAVx5 et iOS ; flux ICS en lecture seule pour Google Calendar et Proton Calendar, "
        "qui ne savent pas parler CalDAV à un serveur tiers.</footer>"
    )
    return _page("Kalendra — administration", "".join(parts))


def _user_page(db, config, request: Request, token: str, user_id: int) -> Response:
    """One account's page: its calendars and the actions that concern it.

    The dashboard now lists a single row per user; all the detail lives here,
    otherwise the landing page becomes unreadable past a handful of accounts.
    """
    user = db.get_user_by_id(user_id)
    if user is None:
        return error(404, "Compte inconnu.")

    base = _base_url(config, request)
    bp = config.base_path
    message = request.query_param("msg")
    calendars = db.list_calendars(user["id"])
    badge = " <span class=badge>admin</span>" if user["is_admin"] else ""

    parts = [
        "<header><h1>Kalendra</h1>"
        f"<span>v{escape(__version__)} · {escape(user['username'])}</span>"
        f"<a href='{bp}/admin' style='margin-left:auto'>← tous les utilisateurs</a>"
        "</header><main>"
    ]
    if message:
        parts.append(f"<div class=flash>{escape(message)}</div>")

    rows = []
    for calendar in calendars:
        count = db.calendar_stats(calendar["id"])
        feed = (
            f"<code>{escape(base)}/feed/{escape(calendar['feed_token'] or '')}.ics</code>"
            if calendar["feed_enabled"] and calendar["feed_token"]
            else "<span class=muted>désactivé</span>"
        )
        vue = f"{bp}/view/{quote(user['username'])}/{quote(calendar['name'])}/"
        rows.append(
            "<tr>"
            f"<td><a href='{vue}'><strong>"
            f"{escape(calendar['display_name'] or calendar['name'])}</strong></a>"
            f"<div class=muted><code>{escape(base)}/calendars/"
            f"{escape(user['username'])}/{escape(calendar['name'])}/</code></div></td>"
            f"<td>{count}</td>"
            f"<td>{feed}</td>"
            f"<td style='white-space:nowrap'>"
            + _button(
                bp, token, "calendars/token", "Régénérer",
                calendar_id=calendar["id"], user_id=user["id"],
            )
            + _button(
                bp, token, "calendars/feed", "Activer/couper",
                calendar_id=calendar["id"], user_id=user["id"],
            )
            + _button(
                bp,
                token,
                "calendars/delete",
                "Supprimer",
                calendar_id=calendar["id"],
                user_id=user["id"],
                cls="danger",
                confirm=True,
            )
            +
            "</td></tr>"
        )

    parts.append(
        f"<section><h2>{escape(user['username'])}{badge} · agendas</h2>"
        "<table><tr><th>Agenda</th><th>Objets</th><th>Flux ICS</th><th></th></tr>"
        + ("".join(rows) or "<tr><td colspan=4 class=muted>Aucun agenda.</td></tr>")
        + "</table>"
        f"<form class=grid method=post action='{bp}/admin/calendars/create'>"
        f"<input type=hidden name=csrf value='{escape(token)}'>"
        f"<input type=hidden name=user_id value='{user['id']}'>"
        "<input name=name placeholder='identifiant (perso)' required pattern='[A-Za-z0-9._-]+'>"
        "<input name=display_name placeholder='nom affiché'>"
        "<input name=color type=color value='#3584e4'>"
        "<button>Ajouter un agenda</button></form></section>"
    )

    carnets = db.list_addressbooks(user["id"])
    lignes_c = []
    for carnet in carnets:
        lignes_c.append(
            "<tr>"
            f"<td><strong>{escape(carnet['display_name'] or carnet['name'])}</strong>"
            f"<div class=muted><code>{escape(base)}/addressbooks/"
            f"{escape(user['username'])}/{escape(carnet['name'])}/</code></div></td>"
            f"<td>{db.calendar_stats(carnet['id'])}</td>"
            f"<td style='white-space:nowrap'>"
            + _button(
                bp,
                token,
                "calendars/delete",
                "Supprimer",
                calendar_id=carnet["id"],
                user_id=user["id"],
                cls="danger",
                confirm=True,
            )
            +
            "</td></tr>"
        )
    parts.append(
        f"<section><h2>{escape(user['username'])} · carnets d'adresses</h2>"
        "<table><tr><th>Carnet</th><th>Contacts</th><th></th></tr>"
        + ("".join(lignes_c) or "<tr><td colspan=3 class=muted>Aucun carnet.</td></tr>")
        + "</table>"
        f"<form class=grid method=post action='{bp}/admin/addressbooks/create'>"
        f"<input type=hidden name=csrf value='{escape(token)}'>"
        f"<input type=hidden name=user_id value='{user['id']}'>"
        "<input name=name placeholder='identifiant (contacts)' required pattern='[A-Za-z0-9._-]+'>"
        "<input name=display_name placeholder='nom affiché'>"
        "<button>Ajouter un carnet</button></form></section>"
    )

    coche = " checked" if user["is_admin"] else ""
    parts.append(
        "<section><h2>Modifier le compte</h2>"
        f"<form class=grid method=post action='{bp}/admin/users/edit'>"
        f"<input type=hidden name=csrf value='{escape(token)}'>"
        f"<input type=hidden name=user_id value='{user['id']}'>"
        f"<input name=display_name placeholder='nom affiché' "
        f"value='{escape(user['display_name'] or '')}'>"
        f"<input name=email type=email placeholder='email' "
        f"value='{escape(user['email'] or '')}'>"
        f"<label class=muted><input type=checkbox name=is_admin{coche}> admin</label>"
        "<button>Enregistrer</button></form>"
        f"<form class=grid method=post action='{bp}/admin/users/password'>"
        f"<input type=hidden name=csrf value='{escape(token)}'>"
        f"<input type=hidden name=user_id value='{user['id']}'>"
        "<input name=password type=password placeholder='nouveau mot de passe' required>"
        "<button class=ghost>Changer le mot de passe</button></form>"
        "<p class=muted>L'identifiant <code>"
        f"{escape(user['username'])}</code> n'est pas modifiable : il figure dans "
        "les URLs CalDAV déjà enregistrées par les clients.</p></section>"
    )

    parts.append(
        "<section><h2>Zone dangereuse</h2>"
        f"<form class=inline method=post action='{bp}/admin/users/delete' "
        "onsubmit=\"return confirm('Supprimer ce compte et tous ses agendas ?')\">"
        f"<input type=hidden name=csrf value='{escape(token)}'>"
        f"<input type=hidden name=user_id value='{user['id']}'>"
        "<button class=danger>Supprimer le compte</button></form>"
        "<p class=muted>Les agendas et leurs objets sont supprimés avec le compte.</p>"
        "</section></main>"
    )
    return _page(f"Kalendra — {user['username']}", "".join(parts))


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
