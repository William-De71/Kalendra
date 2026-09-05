#!/usr/bin/env python3
"""Importe un fichier .ics dans un agenda Kalendra, un objet par événement.

    scripts/import-ics.py --url http://127.0.0.1:5232 --user will \\
        --calendar vacances --file Zone-B.ics

Pourquoi un script et pas une fonctionnalité du serveur : Kalendra n'a aucun
client HTTP sortant et n'en veut pas. Aller chercher un calendrier ailleurs,
c'est une dépendance réseau, un cache, une politique de rafraîchissement et des
erreurs à gérer — tout ce qu'un serveur de stockage n'a pas à porter. L'import
est donc une action que l'administrateur déclenche, ici ou depuis un cron.

Un fichier .ics public agrège tous ses événements dans un seul VCALENDAR, alors
que CalDAV impose une ressource par événement : le script découpe, en
recopiant l'entête VCALENDAR et les VTIMEZONE dans chaque objet produit.

Le contenu n'est pas réécrit au-delà de ce découpage : ni les UID, ni les
propriétés inconnues ne sont touchés, conformément au principe du serveur.
"""

from __future__ import annotations

import argparse
import base64
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kalendra.ics import parse_calendar, split_calendar, wrap_component


def _requete(url: str, methode: str, auth: str, corps: bytes = b"", type_mime: str = "") -> int:
    requete = urllib.request.Request(url, data=corps or None, method=methode)
    requete.add_header("Authorization", auth)
    if type_mime:
        requete.add_header("Content-Type", type_mime)
    try:
        with urllib.request.urlopen(requete) as reponse:
            return reponse.status
    except urllib.error.HTTPError as exc:
        return exc.code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:5232", help="racine du serveur")
    parser.add_argument("--user", required=True, help="compte propriétaire de l'agenda")
    parser.add_argument("--password", required=True)
    parser.add_argument("--calendar", required=True, help="nom de l'agenda cible")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="fichier .ics local")
    source.add_argument("--from-url", help="URL d'un .ics à télécharger d'abord")
    parser.add_argument(
        "--create", action="store_true", help="crée l'agenda s'il n'existe pas"
    )
    parser.add_argument(
        "--prefix", default="", help="préfixe des noms de ressource (défaut : aucun)"
    )
    parser.add_argument("--dry-run", action="store_true", help="n'écrit rien, montre le plan")
    args = parser.parse_args(argv)

    if args.from_url:
        with urllib.request.urlopen(args.from_url) as reponse:
            texte = reponse.read().decode("utf-8", "replace")
    else:
        texte = Path(args.file).read_text(encoding="utf-8", errors="replace")

    preambule, blocs = split_calendar(texte)
    if not blocs:
        print("Aucun événement trouvé dans la source.", file=sys.stderr)
        return 1

    brut = f"{args.user}:{args.password}".encode()
    auth = "Basic " + base64.b64encode(brut).decode("ascii")
    base = args.url.rstrip("/")
    agenda = f"{base}/calendars/{args.user}/{args.calendar}/"

    if args.create and not args.dry_run:
        code = _requete(agenda, "MKCALENDAR", auth)
        if code not in (201, 405):  # 405 = existe déjà
            print(f"Création de l'agenda refusée (HTTP {code}).", file=sys.stderr)
            return 1

    deposes = inchanges = echecs = 0
    for index, bloc in enumerate(blocs, start=1):
        objet = wrap_component(preambule, bloc)
        try:
            # Le premier enfant est le VTIMEZONE recopié du préambule : on
            # cherche explicitement le composant porteur de l'événement.
            composant = next(
                c
                for c in parse_calendar(objet).children
                if c.name in {"VEVENT", "VTODO", "VJOURNAL"}
            )
            uid = composant.value("UID") or f"kalendra-import-{index}"
        except Exception:
            print(f"  bloc {index} illisible, ignoré", file=sys.stderr)
            echecs += 1
            continue

        # Le nom de ressource dérive de l'UID : réimporter le même fichier
        # remplace l'objet au lieu d'en créer un doublon.
        sur = "".join(c if c.isalnum() or c in "._-" else "-" for c in uid)[:200]
        href = f"{args.prefix}{sur}.ics"

        if args.dry_run:
            print(f"  {href}  ({composant.value('SUMMARY', '(sans titre)')})")
            continue

        code = _requete(agenda + href, "PUT", auth, objet.encode("utf-8"), "text/calendar")
        if code == 201:
            deposes += 1
        elif code == 204:
            inchanges += 1
        else:
            print(f"  {href} : HTTP {code}", file=sys.stderr)
            echecs += 1

    if args.dry_run:
        print(f"{len(blocs)} objet(s) seraient déposés dans {agenda}")
        return 0

    print(
        f"{deposes} créé(s), {inchanges} remplacé(s), {echecs} en échec — {agenda}"
    )
    return 1 if echecs else 0


if __name__ == "__main__":
    raise SystemExit(main())
