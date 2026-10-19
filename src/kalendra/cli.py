"""Command-line interface: `kalendra serve`, `user`, `calendar`, `backup`."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from datetime import datetime

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


def cmd_backup(args) -> int:
    db = _database(args)
    destination = args.destination
    if os.path.isdir(destination):
        # A directory means "put it in there": a timestamped name lets a cron
        # job write into the same folder every night without clobbering.
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = os.path.join(destination, f"kalendra-{stamp}.db")
    try:
        size = db.backup(destination, compact=args.compact)
    except FileExistsError:
        raise SystemExit(f"Le fichier existe déjà : {destination}") from None
    print(f"Sauvegarde écrite : {destination} ({size} octets)")
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
        db.create_addressbook(user_id, args.with_addressbook, display_name=args.with_addressbook)
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


def cmd_user_export(args) -> int:
    from .export import export_account

    db = _database(args)
    user = _user_or_exit(db, args.username)
    destination = args.output or f"kalendra-{args.username}.zip"
    if os.path.exists(destination):
        raise SystemExit(f"Le fichier existe déjà : {destination}")
    with open(destination, "wb") as handle:
        handle.write(export_account(db, user))
    print(f"Export écrit : {destination}")
    return 0


def cmd_user_rm(args) -> int:
    db = _database(args)
    user = db.get_user(args.username)
    if user is None:
        raise SystemExit(f"Utilisateur inconnu : {args.username}")
    db.delete_user(user["id"])
    print("Utilisateur supprimé.")
    return 0


def _user_or_exit(db, username: str):
    user = db.get_user(username)
    if user is None:
        raise SystemExit(f"Utilisateur inconnu : {username}")
    return user


def cmd_apppw_add(args) -> int:
    db = _database(args)
    user = _user_or_exit(db, args.username)
    app_id, secret = db.create_app_password(user["id"], args.label)
    print(f"Mot de passe d'application n°{app_id} pour « {args.label} » :")
    print(secret)
    print("Il ne sera plus jamais affiché : saisissez-le maintenant dans le client.")
    return 0


def cmd_apppw_list(args) -> int:
    db = _database(args)
    user = _user_or_exit(db, args.username)
    for row in db.list_app_passwords(user["id"]):
        used = row["last_used_at"] or "jamais utilisé"
        print(f"{row['id']:>3}  {row['label']:<30} créé {row['created_at']}  {used}")
    return 0


def cmd_apppw_rm(args) -> int:
    db = _database(args)
    user = _user_or_exit(db, args.username)
    if not db.delete_app_password(user["id"], args.id):
        raise SystemExit(f"Aucun mot de passe d'application n°{args.id} pour {args.username}")
    print("Mot de passe d'application révoqué.")
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


def _write_or_print(text: str, output: str | None) -> None:
    if output:
        with open(output, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        print(f"Écrit : {output}", file=sys.stderr)
    else:
        sys.stdout.write(text)


def _collection_or_exit(db, username: str, name: str, kind: str):
    user = _user_or_exit(db, username)
    row = db.get_calendar(user["id"], name, kind)
    if row is None:
        quoi = "Carnet" if kind == "addressbook" else "Agenda"
        raise SystemExit(f"{quoi} inconnu : {name}")
    return row


def _import_report(rapport) -> int:
    print(rapport.resume())
    for erreur in rapport.erreurs:
        print(f"  {erreur}", file=sys.stderr)
    return 0 if rapport.total or not rapport.erreurs else 1


def cmd_calendar_export(args) -> int:
    from .export import export_calendar

    db = _database(args)
    _write_or_print(
        export_calendar(db, _collection_or_exit(db, args.username, args.name, "calendar")),
        args.output,
    )
    return 0


def cmd_calendar_import(args) -> int:
    from .importics import importer

    db = _database(args)
    calendar = _collection_or_exit(db, args.username, args.name, "calendar")
    config = Config.from_env()
    with open(args.file, encoding="utf-8", errors="replace") as handle:
        rapport = importer(
            db,
            calendar,
            handle.read(),
            max_taille=config.max_resource_size,
            quota=config.quota_bytes,
        )
    return _import_report(rapport)


def cmd_addressbook_export(args) -> int:
    from .export import export_addressbook

    db = _database(args)
    _write_or_print(
        export_addressbook(db, _collection_or_exit(db, args.username, args.name, "addressbook")),
        args.output,
    )
    return 0


def cmd_addressbook_import(args) -> int:
    from .importvcf import importer_cartes

    db = _database(args)
    carnet = _collection_or_exit(db, args.username, args.name, "addressbook")
    config = Config.from_env()
    with open(args.file, encoding="utf-8", errors="replace") as handle:
        rapport = importer_cartes(
            db,
            carnet,
            handle.read(),
            max_taille=config.max_resource_size,
            quota=config.quota_bytes,
        )
    return _import_report(rapport)


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

    backup = sub.add_parser("backup", help="copie cohérente de la base, serveur en marche")
    backup.add_argument("destination", help="fichier à créer, ou dossier où le créer")
    backup.add_argument(
        "--compact", action="store_true", help="VACUUM INTO : copie plus petite, plus lente"
    )
    backup.set_defaults(func=cmd_backup)

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

    exp = user.add_parser("export", help="tous les agendas et carnets dans un .zip")
    exp.add_argument("username")
    exp.add_argument("-o", "--output", help="fichier .zip (défaut : kalendra-<compte>.zip)")
    exp.set_defaults(func=cmd_user_export)

    remove = user.add_parser("rm")
    remove.add_argument("username")
    remove.set_defaults(func=cmd_user_rm)

    apppw = sub.add_parser(
        "app-password", help="mots de passe d'application (un par appareil)"
    ).add_subparsers(dest="apppw_command", required=True)
    ap_add = apppw.add_parser("add", help="crée et affiche un mot de passe")
    ap_add.add_argument("username")
    ap_add.add_argument("label", help="l'appareil qui l'utilisera (« téléphone »)")
    ap_add.set_defaults(func=cmd_apppw_add)
    ap_list = apppw.add_parser("list")
    ap_list.add_argument("username")
    ap_list.set_defaults(func=cmd_apppw_list)
    ap_rm = apppw.add_parser("rm", help="révoque un mot de passe")
    ap_rm.add_argument("username")
    ap_rm.add_argument("id", type=int)
    ap_rm.set_defaults(func=cmd_apppw_rm)

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

    cal_export = calendar.add_parser("export", help="écrit l'agenda dans un .ics")
    cal_export.add_argument("username")
    cal_export.add_argument("name")
    cal_export.add_argument("-o", "--output", help="fichier (défaut : sortie standard)")
    cal_export.set_defaults(func=cmd_calendar_export)

    cal_import = calendar.add_parser("import", help="verse un .ics dans l'agenda")
    cal_import.add_argument("username")
    cal_import.add_argument("name")
    cal_import.add_argument("file")
    cal_import.set_defaults(func=cmd_calendar_import)

    cal_rm = calendar.add_parser("rm")
    cal_rm.add_argument("username")
    cal_rm.add_argument("name")
    cal_rm.set_defaults(func=cmd_calendar_rm)

    # No "token" subcommand here: an address book has no ICS feed, that
    # format carrying events only.
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

    ab_export = carnet.add_parser("export", help="écrit le carnet dans un .vcf")
    ab_export.add_argument("username")
    ab_export.add_argument("name")
    ab_export.add_argument("-o", "--output", help="fichier (défaut : sortie standard)")
    ab_export.set_defaults(func=cmd_addressbook_export)

    ab_import = carnet.add_parser("import", help="verse un .vcf dans le carnet")
    ab_import.add_argument("username")
    ab_import.add_argument("name")
    ab_import.add_argument("file")
    ab_import.set_defaults(func=cmd_addressbook_import)

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
