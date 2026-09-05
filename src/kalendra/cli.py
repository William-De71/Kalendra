"""Interface en ligne de commande : `kalendra serve`, `user`, `calendar`."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from . import __version__
from .config import Config
from .db import Database


def _database(args) -> Database:
    config = Config.from_env()
    if args.db:
        config.db_path = args.db
    db = Database(config.db_path)
    db.setup()
    return db


def _ask_password(provided: str | None) -> str:
    if provided:
        return provided
    first = getpass.getpass("Mot de passe : ")
    second = getpass.getpass("Confirmation : ")
    if first != second:
        raise SystemExit("Les mots de passe ne correspondent pas.")
    if not first:
        raise SystemExit("Mot de passe vide refusé.")
    return first


# --------------------------------------------------------------- commandes


def cmd_serve(args) -> int:
    from .app import Kalendra
    from .server import serve

    config = Config.from_env()
    if args.db:
        config.db_path = args.db
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    application = Kalendra(config)
    print(
        f"Kalendra {__version__} — CalDAV sur http://{config.host}:{config.port}{config.base_path}/"
        f"\nBase SQLite : {config.db_path}",
        file=sys.stderr,
    )
    serve(application, config.host, config.port)
    return 0


def cmd_init(args) -> int:
    db = _database(args)
    print(f"Base initialisée : {db.path}")
    return 0


def cmd_user_add(args) -> int:
    db = _database(args)
    password = _ask_password(args.password)
    user_id = db.create_user(
        args.username,
        password,
        display_name=args.display_name or args.username,
        email=args.email or "",
        is_admin=args.admin,
    )
    if args.with_calendar:
        db.create_calendar(user_id, args.with_calendar, display_name=args.with_calendar)
    if args.with_addressbook:
        db.create_addressbook(
            user_id, args.with_addressbook, display_name=args.with_addressbook
        )
    print(f"Utilisateur « {args.username} » créé (id={user_id}).")
    return 0


def cmd_user_list(args) -> int:
    db = _database(args)
    for user in db.list_users():
        flag = "admin" if user["is_admin"] else "user "
        calendars = ", ".join(c["name"] for c in db.list_calendars(user["id"])) or "-"
        print(f"{user['id']:>3}  {flag}  {user['username']:<20} agendas: {calendars}")
    return 0


def cmd_user_passwd(args) -> int:
    db = _database(args)
    user = db.get_user(args.username)
    if user is None:
        raise SystemExit(f"Utilisateur inconnu : {args.username}")
    db.set_password(user["id"], _ask_password(args.password))
    print("Mot de passe mis à jour.")
    return 0


def cmd_user_rm(args) -> int:
    db = _database(args)
    user = db.get_user(args.username)
    if user is None:
        raise SystemExit(f"Utilisateur inconnu : {args.username}")
    db.delete_user(user["id"])
    print("Utilisateur supprimé.")
    return 0


def cmd_calendar_add(args) -> int:
    db = _database(args)
    user = db.get_user(args.username)
    if user is None:
        raise SystemExit(f"Utilisateur inconnu : {args.username}")
    calendar_id = db.create_calendar(
        user["id"],
        args.name,
        display_name=args.display_name or args.name,
        description=args.description or "",
        color=args.color,
    )
    calendar = db.get_calendar_by_id(calendar_id)
    print(f"Agenda créé : /calendars/{args.username}/{args.name}/")
    print(f"Flux ICS   : /feed/{calendar['feed_token']}.ics")
    return 0


def cmd_calendar_list(args) -> int:
    db = _database(args)
    for calendar in db.list_all_calendars():
        count = db.calendar_stats(calendar["id"])
        feed = calendar["feed_token"] if calendar["feed_enabled"] else "(désactivé)"
        print(
            f"{calendar['id']:>3}  /calendars/{calendar['username']}/{calendar['name']}/"
            f"  objets={count:<5} flux={feed}"
        )
    return 0


def cmd_calendar_token(args) -> int:
    db = _database(args)
    user = db.get_user(args.username)
    if user is None:
        raise SystemExit(f"Utilisateur inconnu : {args.username}")
    calendar = db.get_calendar(user["id"], args.name)
    if calendar is None:
        raise SystemExit(f"Agenda inconnu : {args.name}")
    token = db.rotate_feed_token(calendar["id"])
    print(f"/feed/{token}.ics")
    return 0


def cmd_calendar_rm(args) -> int:
    db = _database(args)
    user = db.get_user(args.username)
    if user is None:
        raise SystemExit(f"Utilisateur inconnu : {args.username}")
    calendar = db.get_calendar(user["id"], args.name)
    if calendar is None:
        raise SystemExit(f"Agenda inconnu : {args.name}")
    db.delete_calendar(calendar["id"])
    print("Agenda supprimé.")
    return 0


def cmd_addressbook_add(args) -> int:
    db = _database(args)
    user = db.get_user(args.username)
    if user is None:
        raise SystemExit(f"Utilisateur inconnu : {args.username}")
    db.create_addressbook(
        user["id"],
        args.name,
        display_name=args.display_name or args.name,
        description=args.description or "",
    )
    print(f"Carnet créé : /addressbooks/{args.username}/{args.name}/")
    return 0


def cmd_addressbook_list(args) -> int:
    db = _database(args)
    for user in db.list_users():
        for carnet in db.list_addressbooks(user["id"]):
            count = db.calendar_stats(carnet["id"])
            print(
                f"{carnet['id']:>3}  /addressbooks/{user['username']}/{carnet['name']}/"
                f"  contacts={count}"
            )
    return 0


def cmd_addressbook_rm(args) -> int:
    db = _database(args)
    user = db.get_user(args.username)
    if user is None:
        raise SystemExit(f"Utilisateur inconnu : {args.username}")
    carnet = db.get_calendar(user["id"], args.name, "addressbook")
    if carnet is None:
        raise SystemExit(f"Carnet inconnu : {args.name}")
    db.delete_calendar(carnet["id"])
    print("Carnet supprimé.")
    return 0


# ------------------------------------------------------------------ parseur


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kalendra", description="Serveur CalDAV autonome sur SQLite."
    )
    parser.add_argument("--version", action="version", version=f"kalendra {__version__}")
    parser.add_argument("--db", help="chemin de la base SQLite (défaut : $KALENDRA_DB)")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="démarre le serveur HTTP")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.set_defaults(func=cmd_serve)

    sub.add_parser("init-db", help="crée le schéma SQLite").set_defaults(func=cmd_init)

    user = sub.add_parser("user", help="gestion des comptes").add_subparsers(
        dest="user_command", required=True
    )
    add = user.add_parser("add")
    add.add_argument("username")
    add.add_argument("--password")
    add.add_argument("--display-name")
    add.add_argument("--email")
    add.add_argument("--admin", action="store_true")
    add.add_argument("--with-calendar", metavar="NOM", help="crée aussi un agenda")
    add.add_argument("--with-addressbook", metavar="NOM", help="crée aussi un carnet")
    add.set_defaults(func=cmd_user_add)

    user.add_parser("list").set_defaults(func=cmd_user_list)

    passwd = user.add_parser("passwd")
    passwd.add_argument("username")
    passwd.add_argument("--password")
    passwd.set_defaults(func=cmd_user_passwd)

    remove = user.add_parser("rm")
    remove.add_argument("username")
    remove.set_defaults(func=cmd_user_rm)

    calendar = sub.add_parser("calendar", help="gestion des agendas").add_subparsers(
        dest="calendar_command", required=True
    )
    cal_add = calendar.add_parser("add")
    cal_add.add_argument("username")
    cal_add.add_argument("name")
    cal_add.add_argument("--display-name")
    cal_add.add_argument("--description")
    cal_add.add_argument("--color", default="#3584e4")
    cal_add.set_defaults(func=cmd_calendar_add)

    calendar.add_parser("list").set_defaults(func=cmd_calendar_list)

    cal_token = calendar.add_parser("token", help="régénère le jeton de flux ICS")
    cal_token.add_argument("username")
    cal_token.add_argument("name")
    cal_token.set_defaults(func=cmd_calendar_token)

    cal_rm = calendar.add_parser("rm")
    cal_rm.add_argument("username")
    cal_rm.add_argument("name")
    cal_rm.set_defaults(func=cmd_calendar_rm)

    # Pas de sous-commande « token » ici : un carnet n'a pas de flux ICS, ce
    # format ne transportant que des événements.
    carnet = sub.add_parser("addressbook", help="gestion des carnets d'adresses").add_subparsers(
        dest="addressbook_command", required=True
    )
    ab_add = carnet.add_parser("add")
    ab_add.add_argument("username")
    ab_add.add_argument("name")
    ab_add.add_argument("--display-name")
    ab_add.add_argument("--description")
    ab_add.set_defaults(func=cmd_addressbook_add)

    carnet.add_parser("list").set_defaults(func=cmd_addressbook_list)

    ab_rm = carnet.add_parser("rm")
    ab_rm.add_argument("username")
    ab_rm.add_argument("name")
    ab_rm.set_defaults(func=cmd_addressbook_rm)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
