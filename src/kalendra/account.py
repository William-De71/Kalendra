"""The "Mon compte" page under /view/compte/: what an account needs to set up
its own devices without asking an administrator.

Three things live here. The exact URLs to type into each client, because
discovery is where people get stuck. A configuration profile for Apple
devices, which fills those URLs in. And app passwords, one per device, so a
lost phone is revoked on its own.

Every account reaches this page for itself only: nothing here takes a
username from the request, so there is no other account to aim at.
"""

from __future__ import annotations

import plistlib
import re
import uuid
from html import escape
from urllib.parse import parse_qs, quote, urlsplit

from . import __version__
from .http import Request, Response, error, text_response
from .security import csrf_valid
from .webui import BASE_STYLE, public_base_url, rail, titre

STYLE = """
main { padding:20px 24px 40px; max-width:900px; }
section.bloc { background:var(--card); border:1px solid var(--line); border-radius:12px;
  padding:18px 20px; margin-bottom:18px; }
section.bloc h2 { margin:0 0 4px; font-size:16px; }
section.bloc > p.muted { margin:0 0 14px; }
.muted { color:var(--muted); font-size:13.5px; }
dl.urls { display:grid; grid-template-columns:auto 1fr; gap:8px 16px; margin:0; }
dl.urls dt { color:var(--muted); font-size:13px; padding-top:3px; }
dl.urls dd { margin:0; }
code { font:12.5px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
  background:var(--bg); border:1px solid var(--line); border-radius:6px;
  padding:2px 6px; word-break:break-all; }
details.client { border-top:1px solid var(--line); padding:10px 0 2px; }
details.client:first-of-type { margin-top:14px; }
details.client summary { cursor:pointer; font-weight:600; font-size:14px; }
details.client ol { margin:8px 0 6px; padding-left:22px; font-size:14px; }
details.client li { margin:3px 0; }
table.mdp { width:100%; border-collapse:collapse; font-size:14px; margin-bottom:12px; }
table.mdp th { text-align:left; color:var(--muted); font-weight:500; font-size:12.5px;
  padding:6px 8px; border-bottom:1px solid var(--line); }
table.mdp td { padding:8px; border-bottom:1px solid var(--line); }
table.mdp td:last-child { text-align:right; }
form.ligne { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
form.ligne input[type=text] { font:inherit; padding:7px 10px; border:1px solid var(--line);
  border-radius:8px; background:var(--bg); color:var(--fg); min-width:0; flex:1; }
button { font:inherit; padding:7px 13px; border-radius:8px; cursor:pointer;
  border:1px solid transparent; background:var(--accent); color:#fff; }
button.ghost { background:transparent; border-color:var(--line); color:var(--danger); }
a.bouton { display:inline-block; text-decoration:none; padding:7px 13px; border-radius:8px;
  background:var(--accent); color:#fff; font-size:14px; }
.secret { border:1px solid var(--accent); border-radius:10px; padding:12px 14px;
  margin-bottom:14px; background:var(--bg); }
.secret code { font-size:15px; display:inline-block; margin:6px 0; user-select:all; }
.flash { padding:10px 14px; border-radius:10px; margin:0 0 14px; border:1px solid var(--line);
  background:var(--card); }
.jauge { height:8px; border-radius:4px; background:var(--line); overflow:hidden;
  margin:10px 0 6px; }
.jauge span { display:block; height:100%; background:var(--accent); }
@media (max-width:640px) { main { padding:14px; }
  dl.urls { grid-template-columns:1fr; } }
"""


def _page(title: str, body: str, config, user_row) -> Response:
    html = (
        "<!doctype html><html lang=fr><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title><style>{BASE_STYLE}{STYLE}</style></head><body>"
        + rail(
            config.base_path, "compte", bool(user_row["is_admin"]),
            user_row["username"], __version__,
        )
        + f"<div class=zone>{body}</div></body></html>"
    )
    response = text_response(200, html, "text/html; charset=utf-8")
    # The page may carry a freshly created secret: keep it out of every cache.
    return response.header("Cache-Control", "no-store")


def _taille(octets: int) -> str:
    for unite, facteur in (("Go", 1024**3), ("Mo", 1024**2), ("ko", 1024)):
        if octets >= facteur:
            return f"{octets / facteur:.1f} {unite}".replace(".", ",")
    return f"{octets} octets"


def _clients(racine: str, username: str) -> str:
    """Per-client steps: the URL to type differs, and that is the whole difficulty."""
    caldav = f"{racine}/calendars/{quote(username)}/"
    carddav = f"{racine}/addressbooks/{quote(username)}/"
    u = escape(username)
    etapes = (
        (
            "GNOME Evolution",
            [
                "Fichier → Nouveau → Agenda, type <b>CalDAV</b>.",
                f"URL : <code>{escape(racine)}/</code>, utilisateur <code>{u}</code>.",
                (
                    "« Rechercher des agendas » liste vos agendas ; même démarche avec "
                    "Fichier → Nouveau → Carnet d'adresses, type <b>CardDAV</b>."
                ),
            ],
        ),
        (
            "Thunderbird",
            [
                "Agenda → Nouvel agenda → Sur le réseau.",
                (
                    f"Utilisateur <code>{u}</code>, adresse <code>{escape(racine)}/</code> : "
                    "Thunderbird découvre agendas et carnets d'adresses."
                ),
            ],
        ),
        (
            "Android (DAVx⁵)",
            [
                "Ajouter un compte → « Connexion avec une URL et un nom d'utilisateur ».",
                f"URL de base : <code>{escape(racine)}/</code>, utilisateur <code>{u}</code>.",
                "Cochez ensuite les agendas et carnets à synchroniser.",
            ],
        ),
        (
            "iPhone, iPad, Mac",
            [
                (
                    "Le plus simple : installez le profil ci-dessous, il remplit tout sauf "
                    "le mot de passe."
                ),
                (
                    "À la main : Réglages → Calendrier → Comptes → Ajouter un compte → Autre "
                    f"→ Compte CalDAV, serveur <code>{escape(urlsplit(racine).netloc)}</code>, "
                    f"utilisateur <code>{u}</code>. Idem pour les contacts avec un compte CardDAV."
                ),
            ],
        ),
    )
    blocs = "".join(
        f"<details class=client><summary>{titre_client}</summary><ol>"
        + "".join(f"<li>{ligne}</li>" for ligne in lignes)
        + "</ol></details>"
        for titre_client, lignes in etapes
    )
    return (
        "<dl class=urls>"
        f"<dt>Serveur</dt><dd><code>{escape(racine)}/</code></dd>"
        f"<dt>Agendas (CalDAV)</dt><dd><code>{escape(caldav)}</code></dd>"
        f"<dt>Contacts (CardDAV)</dt><dd><code>{escape(carddav)}</code></dd>"
        f"<dt>Utilisateur</dt><dd><code>{u}</code></dd>"
        "</dl>" + blocs
    )


def rendre_compte(
    db, config, request: Request, user_row, token: str, secret: tuple[str, str] | None = None,
    message: str = "",
) -> Response:
    base = config.base_path
    racine = public_base_url(config, request)
    ici = f"{base}/view/compte/"

    nouveau = ""
    if secret is not None:
        libelle, valeur = secret
        nouveau = (
            "<div class=secret>"
            f"Mot de passe pour <b>{escape(libelle)}</b> :<br><code>{escape(valeur)}</code><br>"
            "<span class=muted>Saisissez-le maintenant dans l'appareil, avec "
            f"l'utilisateur <code>{escape(user_row['username'])}</code>. Il ne sera plus "
            "jamais affiché.</span></div>"
        )

    lignes = []
    for row in db.list_app_passwords(user_row["id"]):
        utilise = (row["last_used_at"] or "")[:16].replace("T", " ") or "jamais"
        lignes.append(
            "<tr>"
            f"<td>{escape(row['label'])}</td>"
            f"<td class=muted>{escape(row['created_at'][:10])}</td>"
            f"<td class=muted>{escape(utilise)}</td>"
            f"<td><form method=post action='{ici}'>"
            f"<input type=hidden name=csrf value='{escape(token)}'>"
            "<input type=hidden name=action value=revoquer>"
            f"<input type=hidden name=id value='{int(row['id'])}'>"
            "<button class=ghost>Révoquer</button></form></td>"
            "</tr>"
        )
    tableau = (
        "<table class=mdp><tr><th>Appareil</th><th>Créé le</th><th>Dernière utilisation (UTC)"
        "</th><th></th></tr>" + "".join(lignes) + "</table>"
        if lignes
        else "<p class=muted>Aucun mot de passe d'application pour l'instant.</p>"
    )

    utilise = db.account_usage(user_row["id"])
    quota = config.quota_bytes
    if quota > 0:
        part = min(100, round(100 * utilise / quota)) if quota else 0
        espace = (
            f"<p>{_taille(utilise)} utilisés sur {_taille(quota)}.</p>"
            f"<div class=jauge><span style='width:{part}%'></span></div>"
        )
    else:
        espace = f"<p>{_taille(utilise)} utilisés, sans limite fixée sur ce serveur.</p>"

    corps = (
        titre("<h1>Mon compte</h1>", "")
        + "<main>"
        + (f"<div class=flash>{escape(message)}</div>" if message else "")
        + "<section class=bloc><h2>Configurer vos appareils</h2>"
        "<p class=muted>Les adresses à saisir dans votre client d'agenda ou de "
        "contacts.</p>"
        + _clients(racine, user_row["username"])
        + "</section>"
        "<section class=bloc><h2>Profil pour iPhone, iPad et Mac</h2>"
        "<p class=muted>Installe en une fois le compte d'agendas et le compte de "
        "contacts. Le mot de passe est demandé à l'installation : utilisez un mot "
        "de passe d'application créé ci-dessous.</p>"
        f"<a class=bouton href='{ici}?profil=apple' download>"
        "Télécharger le profil (.mobileconfig)</a></section>"
        "<section class=bloc id=mots-de-passe><h2>Mots de passe d'application</h2>"
        "<p class=muted>Un par appareil : en cas de perte, on révoque celui-là sans "
        "changer le mot de passe du compte. Ils ouvrent la synchronisation CalDAV et "
        "CardDAV, jamais cette interface.</p>"
        + nouveau
        + tableau
        + f"<form method=post action='{ici}' class=ligne>"
        f"<input type=hidden name=csrf value='{escape(token)}'>"
        "<input type=hidden name=action value=creer>"
        "<input type=text name=label maxlength=80 required "
        "placeholder='Nom de l’appareil (téléphone, portable…)'>"
        "<button>Créer</button></form></section>"
        "<section class=bloc><h2>Espace utilisé</h2>" + espace + "</section>"
        "</main>"
    )
    return _page("Kalendra — mon compte", corps, config, user_row)


def profil_apple(config, request: Request, user_row) -> Response:
    """A configuration profile holding a CalDAV and a CardDAV account.

    No password goes in: iOS asks for it on installation, and a profile file
    with a secret inside tends to linger in download folders. UUIDs derive from
    the server and the account, so re-installing replaces the profile instead
    of adding a second one.
    """
    racine = public_base_url(config, request)
    parts = urlsplit(racine)
    https = parts.scheme == "https"
    port = parts.port or (443 if https else 80)
    hote = parts.hostname or "localhost"
    username = user_row["username"]
    principal = f"{parts.path}/principals/{quote(username)}/"
    graine = f"kalendra:{racine}:{username}"
    ident = "kalendra." + re.sub(r"[^A-Za-z0-9.-]", "-", f"{hote}.{username}")

    def compte(nature: str, prefixe: str) -> dict:
        return {
            "PayloadType": f"com.apple.{nature}.account",
            "PayloadVersion": 1,
            "PayloadIdentifier": f"{ident}.{nature}",
            "PayloadUUID": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{graine}:{nature}")).upper(),
            "PayloadDisplayName": f"Kalendra ({nature})",
            f"{prefixe}AccountDescription": f"Kalendra — {username}",
            f"{prefixe}HostName": hote,
            f"{prefixe}Port": port,
            f"{prefixe}UseSSL": https,
            f"{prefixe}Username": username,
            f"{prefixe}PrincipalURL": principal,
        }

    profil = {
        "PayloadType": "Configuration",
        "PayloadVersion": 1,
        "PayloadIdentifier": ident,
        "PayloadUUID": str(uuid.uuid5(uuid.NAMESPACE_URL, graine)).upper(),
        "PayloadDisplayName": f"Kalendra — {username}",
        "PayloadDescription": "Agendas et contacts synchronisés avec Kalendra.",
        "PayloadRemovalDisallowed": False,
        "PayloadContent": [compte("caldav", "CalDAV"), compte("carddav", "CardDAV")],
    }
    corps = plistlib.dumps(profil)
    nom = re.sub(r"[^A-Za-z0-9._-]", "_", username)
    return Response(
        200,
        corps,
        [
            ("Content-Type", "application/x-apple-aspen-config"),
            ("Content-Length", str(len(corps))),
            ("Content-Disposition", f'attachment; filename="kalendra-{nom}.mobileconfig"'),
        ],
    )


def handle_account(db, config, request: Request, token: str) -> Response:
    user_row = request.user
    if request.method in {"GET", "HEAD"}:
        if request.query_param("profil") == "apple":
            return profil_apple(config, request, user_row)
        return rendre_compte(
            db, config, request, user_row, token, message=request.query_param("msg")
        )
    if request.method != "POST":
        return error(405, "Méthode non autorisée.").header("Allow", "GET, HEAD, POST")

    champs = {
        cle: valeurs[0]
        for cle, valeurs in parse_qs(request.text, keep_blank_values=True).items()
    }
    if not csrf_valid(db.secret_key(), user_row["username"], champs.get("csrf", "")):
        return error(403, "Jeton CSRF invalide.")

    action = champs.get("action", "")
    if action == "creer":
        libelle = champs.get("label", "").strip()
        try:
            _, secret = db.create_app_password(user_row["id"], libelle)
        except ValueError as exc:
            return _rediriger(config, str(exc).capitalize() + ".")
        # Answered directly rather than redirected: the secret must not travel
        # in a URL, where history and logs would keep it.
        return rendre_compte(db, config, request, user_row, token, secret=(libelle, secret))
    if action == "revoquer":
        try:
            ident = int(champs.get("id", ""))
        except ValueError:
            return _rediriger(config, "Mot de passe introuvable.")
        if db.delete_app_password(user_row["id"], ident):
            return _rediriger(config, "Mot de passe d'application révoqué.")
        return _rediriger(config, "Mot de passe introuvable.")
    return error(400, "Action inconnue.")


def _rediriger(config, message: str) -> Response:
    cible = f"{config.base_path}/view/compte/?msg={quote(message)}#mots-de-passe"
    return Response(303, b"", [("Location", cible), ("Content-Length", "0")])
