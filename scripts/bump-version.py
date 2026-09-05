#!/usr/bin/env python3
"""Calcule la version suivante et met à jour les fichiers qui la portent.

    scripts/bump-version.py patch|minor|major [--dry-run]

Sert au workflow « Préparer une version » : GitHub ne connaît que le niveau
choisi (patch, minor, major), c'est ici qu'on en déduit le numéro et qu'on
réécrit `__init__.py` et `CHANGELOG.md`.

Un script plutôt que du shell dans le workflow : la manipulation du journal des
modifications demande de déplacer une section entière, ce qui est illisible en
`sed` et impossible à tester hors de GitHub.

Le script n'appelle jamais git : il prépare les fichiers, et c'est au workflow
(ou à l'utilisateur) de committer et d'étiqueter.
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import re
import sys

RACINE = pathlib.Path(__file__).resolve().parents[1]
INIT = RACINE / "src" / "kalendra" / "__init__.py"
CHANGELOG = RACINE / "CHANGELOG.md"
PYPROJECT = RACINE / "pyproject.toml"

VERSION_RE = re.compile(r'^__version__ = "(\d+)\.(\d+)\.(\d+)"$', re.MULTILINE)
#: `pyproject.toml` porte la version en dur, séparément de `__init__.py` : les
#: deux doivent bouger ensemble, sans quoi la roue publiée annoncerait un
#: numéro différent de celui que le serveur affiche.
PYPROJECT_RE = re.compile(r'^version = "(\d+\.\d+\.\d+)"$', re.MULTILINE)


def version_courante() -> tuple[int, int, int]:
    trouve = VERSION_RE.search(INIT.read_text(encoding="utf-8"))
    if trouve is None:
        raise SystemExit(f"__version__ introuvable ou mal formé dans {INIT}")

    # Les deux fichiers doivent déjà s'accorder : partir d'un dépôt incohérent
    # ne ferait que propager l'écart à la version suivante.
    dans_pyproject = PYPROJECT_RE.search(PYPROJECT.read_text(encoding="utf-8"))
    courante = ".".join(trouve.groups())
    if dans_pyproject is not None and dans_pyproject.group(1) != courante:
        raise SystemExit(
            f"__init__.py annonce {courante} mais pyproject.toml "
            f"{dans_pyproject.group(1)} : accordez-les avant de publier."
        )
    return tuple(int(g) for g in trouve.groups())  # type: ignore[return-value]


def version_suivante(actuelle: tuple[int, int, int], niveau: str) -> tuple[int, int, int]:
    majeur, mineur, correctif = actuelle
    if niveau == "major":
        return majeur + 1, 0, 0
    if niveau == "minor":
        return majeur, mineur + 1, 0
    return majeur, mineur, correctif + 1


def _section_non_publiee(texte: str) -> str:
    """Contenu de « Non publié », sans son titre."""
    debut = texte.find("## [Non publié]")
    if debut < 0:
        return ""
    apres = texte.index("\n", debut) + 1
    suivante = texte.find("\n## [", apres)
    fin = suivante + 1 if suivante >= 0 else len(texte)
    return texte[apres:fin].strip("\n")


def maj_changelog(texte: str, version: str, aujourdhui: str) -> str:
    """Bascule « Non publié » vers une section datée, et remonte les liens.

    Si « Non publié » est vide, on refuse : publier une version sans avoir
    décrit ce qu'elle change rendrait les notes de release vides, et
    `release.yml` les extrait telles quelles.
    """
    contenu = _section_non_publiee(texte)
    if not contenu.strip():
        raise SystemExit(
            "La section « Non publié » du CHANGELOG est vide : "
            "décrivez les changements avant de publier."
        )

    debut = texte.find("## [Non publié]")
    apres_titre = texte.index("\n", debut) + 1
    suivante = texte.find("\n## [", apres_titre)
    fin = suivante + 1 if suivante >= 0 else len(texte)

    remplacement = (
        "## [Non publié]\n\n"
        f"## [{version}] — {aujourdhui}\n\n"
        f"{contenu}\n\n"
    )
    texte = texte[:debut] + remplacement + texte[fin:]

    # Les liens de comparaison en pied de fichier : « Non publié » repart de la
    # version qu'on vient de figer, et la nouvelle pointe vers la précédente.
    lien_non_publie = re.search(
        r"^\[Non publié\]: (\S+)/compare/v(\d+\.\d+\.\d+)\.\.\.HEAD$", texte, re.MULTILINE
    )
    if lien_non_publie is not None:
        base, precedente = lien_non_publie.group(1), lien_non_publie.group(2)
        texte = texte.replace(
            lien_non_publie.group(0),
            f"[Non publié]: {base}/compare/v{version}...HEAD\n"
            f"[{version}]: {base}/compare/v{precedente}...v{version}",
        )
    return texte


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "niveau", nargs="?", choices=["patch", "minor", "major"],
        help="omis avec --show",
    )
    parser.add_argument(
        "--show", action="store_true", help="affiche la version courante et sort"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="affiche la version sans rien écrire"
    )
    args = parser.parse_args(argv)

    actuelle = version_courante()
    if args.show:
        print(".".join(str(n) for n in actuelle))
        return 0
    if args.niveau is None:
        parser.error("indiquez un niveau (patch, minor, major) ou --show")
    suivante = version_suivante(actuelle, args.niveau)
    version = ".".join(str(n) for n in suivante)

    if args.dry_run:
        print(version)
        return 0

    # Tout calculer avant d'écrire quoi que ce soit : `maj_changelog` refuse une
    # section « Non publié » vide, et une écriture partielle laisserait
    # `__version__` incrémenté sans section correspondante — exactement
    # l'incohérence que `release.yml` refuse ensuite de publier.
    aujourdhui = datetime.date.today().isoformat()
    nouveau_init = VERSION_RE.sub(
        f'__version__ = "{version}"', INIT.read_text(encoding="utf-8"), count=1
    )
    nouveau_changelog = maj_changelog(
        CHANGELOG.read_text(encoding="utf-8"), version, aujourdhui
    )
    texte_pyproject = PYPROJECT.read_text(encoding="utf-8")
    if PYPROJECT_RE.search(texte_pyproject) is None:
        raise SystemExit(f"version introuvable ou mal formée dans {PYPROJECT}")
    nouveau_pyproject = PYPROJECT_RE.sub(f'version = "{version}"', texte_pyproject, count=1)

    INIT.write_text(nouveau_init, encoding="utf-8")
    CHANGELOG.write_text(nouveau_changelog, encoding="utf-8")
    PYPROJECT.write_text(nouveau_pyproject, encoding="utf-8")

    ancienne = ".".join(str(n) for n in actuelle)
    print(f"{ancienne} -> {version}", file=sys.stderr)
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
